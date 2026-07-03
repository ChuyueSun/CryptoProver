#!/usr/bin/env python3
"""Tail a stream-json round file and print one pretty line per event.

Used by live_run.sh to show the agent's workflow as it happens.
Exits when the final `type:"result"` event arrives, or after `--wait` seconds
with no file. All output is line-buffered.

  python3 _live_stream.py path/to/round_1.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# ANSI
C_DIM    = "\033[2m"
C_BOLD   = "\033[1m"
C_RESET  = "\033[0m"
C_GREEN  = "\033[32m"
C_BGREEN = "\033[92m"
C_RED    = "\033[31m"
C_YELLOW = "\033[33m"
C_BLUE   = "\033[34m"
C_CYAN   = "\033[36m"
C_BCYAN  = "\033[96m"
C_MAG    = "\033[35m"
C_BMAG   = "\033[95m"
C_GREY   = "\033[90m"


def shorten(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def emit_tool_use(t: str, name: str, inp: dict) -> None:
    """One line per tool the agent calls."""
    if name == "Bash":
        cmd = inp.get("command", "")
        # verus_check is special — it's the SMT solver. Always show as 🧮.
        if "verus_check" in cmd or "cargo verus" in cmd:
            print(f"{t} {C_BCYAN}🧮 verus_check{C_RESET}  {C_DIM}(SMT verify){C_RESET}", flush=True)
        else:
            m = re.search(r"skills/(\w+)\.py\s*(.*)", cmd)
            if m:
                skill, args = m.group(1), shorten(m.group(2), 70)
                print(f"{t} {C_MAG}🔍 {skill}{C_RESET}  {C_DIM}{args}{C_RESET}", flush=True)
            else:
                head = shorten(cmd.split("&&")[0].split("|")[0], 70)
                print(f"{t} {C_GREY}📂 shell{C_RESET}     {C_DIM}{head}{C_RESET}", flush=True)
    elif name == "Edit":
        path = inp.get("file_path", "").split("/")[-1]
        old = shorten(inp.get("old_string", ""), 50)
        print(f"{t} {C_YELLOW}✏️  Edit{C_RESET} {path}  {C_DIM}{old}{C_RESET}", flush=True)
    elif name == "Write":
        path = inp.get("file_path", "").split("/")[-1]
        print(f"{t} {C_YELLOW}✏️  Write{C_RESET} {path}", flush=True)
    elif name == "Read":
        path = inp.get("file_path", "").split("/")[-1]
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset and limit:
            spec = f":{offset}-{offset + limit}"
        elif limit:
            spec = f" ({limit} lines)"
        else:
            spec = ""
        print(f"{t} {C_BLUE}📖 Read{C_RESET}     {path}{spec}", flush=True)
    elif name == "Grep":
        pat = shorten(inp.get("pattern", ""), 50)
        print(f"{t} {C_MAG}🔍 Grep{C_RESET}     '{pat}'", flush=True)
    elif name == "Glob":
        pat = inp.get("pattern", "")
        print(f"{t} {C_MAG}🔍 Glob{C_RESET}     '{pat}'", flush=True)
    elif name == "TodoWrite":
        todos = inp.get("todos", [])
        in_prog = [d.get("content", "") for d in todos if d.get("status") == "in_progress"]
        active = shorten(in_prog[0], 60) if in_prog else f"{len(todos)} tasks"
        print(f"{t} {C_GREY}📋 TodoWrite{C_RESET} {C_DIM}{active}{C_RESET}", flush=True)
    elif name == "Agent":
        desc = shorten(inp.get("description", ""), 60)
        print(f"{t} {C_BMAG}🤝 Subagent{C_RESET}  {C_DIM}{desc}{C_RESET}", flush=True)
    elif name == "ToolSearch":
        q = shorten(inp.get("query", ""), 50)
        print(f"{t} {C_GREY}🔧 ToolSearch{C_RESET} {C_DIM}{q}{C_RESET}", flush=True)
    else:
        print(f"{t} {C_GREY}🔧 {name}{C_RESET}", flush=True)


def emit_tool_result(t: str, content, is_error: bool) -> None:
    """Continuation line under the tool_use line."""
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    elif not isinstance(content, str):
        content = json.dumps(content)
    body = shorten(content, 90)
    if is_error:
        print(f"      {C_RED}↳ ❌ {body}{C_RESET}", flush=True)
    else:
        print(f"      {C_DIM}↳ ✅ {body}{C_RESET}", flush=True)


def emit_assistant_text(t: str, text: str) -> None:
    text = shorten(text, 110)
    if text:
        print(f"{t} {C_CYAN}💬 {text}{C_RESET}", flush=True)


def emit_thinking(t: str, text: str) -> None:
    # First non-empty line of the thinking block
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            print(f"{t} {C_GREY}💭 {shorten(ln, 110)}{C_RESET}", flush=True)
            return


def process(ev: dict, start: float) -> bool:
    """Process one event. Return True to stop (final result seen)."""
    elapsed = int(time.time() - start)
    t = f"{C_DIM}[+{elapsed:>3}s]{C_RESET}"
    typ = ev.get("type")
    if typ == "system" and ev.get("subtype") == "init":
        print(f"{t} {C_BOLD}{C_BGREEN}🚀 agent session started{C_RESET}", flush=True)
    elif typ == "assistant":
        for block in ev.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "tool_use":
                emit_tool_use(t, block.get("name", "?"), block.get("input", {}))
            elif bt == "text":
                emit_assistant_text(t, block.get("text", ""))
            elif bt == "thinking":
                emit_thinking(t, block.get("thinking", ""))
    elif typ == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                emit_tool_result(t, block.get("content", ""), block.get("is_error", False))
    elif typ == "result":
        subtype = ev.get("subtype", "?")
        cost = ev.get("total_cost_usd", 0.0)
        dur_s = ev.get("duration_ms", 0) / 1000
        color = C_BGREEN if subtype == "success" else C_RED
        print(
            f"\n{t} {C_BOLD}{color}🎉 result: {subtype}{C_RESET}  "
            f"duration={dur_s:.0f}s  cost=${cost:.2f}",
            flush=True,
        )
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--wait", type=int, default=60,
                    help="Seconds to wait for file to appear (default 60)")
    args = ap.parse_args()

    start = time.time()

    # Wait for file to appear
    deadline = start + args.wait
    while not os.path.exists(args.path):
        if time.time() > deadline:
            print(f"{C_RED}timeout: {args.path} did not appear within {args.wait}s{C_RESET}",
                  file=sys.stderr, flush=True)
            return 2
        time.sleep(0.2)

    # Tail it
    with open(args.path, "r", encoding="utf-8", errors="replace") as f:
        buf = ""
        while True:
            chunk = f.readline()
            if not chunk:
                # No new data — small sleep to avoid busy loop
                time.sleep(0.15)
                continue
            buf += chunk
            if not buf.endswith("\n"):
                continue  # partial line, wait for the rest
            line, buf = buf.rstrip("\n"), ""
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if process(ev, start):
                return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
