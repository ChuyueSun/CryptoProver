#!/usr/bin/env python3
"""Pretty-print a Claude Code stream-json NDJSON file, event by event.

Usage:
    python replay.py <round_N.jsonl>
    python replay.py <round_N.jsonl> --compact        # default: truncate noisy tool outputs
    python replay.py <round_N.jsonl> --full           # nothing truncated
    python replay.py <round_N.jsonl> --only tool_use  # filter by event class
    python replay.py <round_N.jsonl> --since 120      # skip first N seconds of events
    python replay.py <round_N.jsonl> --no-color       # for logging / piping

Event classes (use with --only, comma-sep for multiple):
    system        session init, task notifications
    assistant     Claude text + tool-use requests
    tool_use      just the tool invocations Claude makes
    tool_result   just the results coming back
    result        the final one-line summary

Keys q / space / arrow-down: not supported (this is not an interactive pager).
Pipe into `less -R` for pagination with colors:

    python replay.py round_1.jsonl | less -R
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


# ------------- ANSI colors (plain ANSI, no deps) -------------

class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    # Foreground
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"


_use_color = True


def c(color: str, text: str) -> str:
    if not _use_color:
        return text
    return f"{color}{text}{C.RESET}"


def rule(title: str = "", color: str = C.GREY, width: int = 78) -> str:
    if title:
        pad = max(0, width - len(title) - 4)
        line = f"── {title} " + "─" * pad
    else:
        line = "─" * width
    return c(color, line)


# ------------- event rendering -------------

def format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    return f"{s/60:.1f}m"


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + c(C.DIM, f"\n  [... {len(text) - max_chars} more chars truncated]")


def render_system(ev: dict) -> str:
    sub = ev.get("subtype", "")
    if sub == "init":
        tools = ev.get("tools", [])
        model = ev.get("model", "?")
        sid = ev.get("session_id", "?")[:8]
        return (
            c(C.BRIGHT_BLUE, f"[system/init] ")
            + f"model={model} session={sid} tools={len(tools)}"
        )
    return c(C.BLUE, f"[system/{sub}]") + f" {json.dumps(ev, indent=None)[:200]}"


def render_tool_use(tool: dict, max_input_chars: int) -> list[str]:
    """Render one tool_use content block."""
    name = tool.get("name", "?")
    tid = tool.get("id", "?")[-6:]
    inp = tool.get("input", {})
    lines = [c(C.BRIGHT_MAGENTA, f"  🔧 {name}") + c(C.DIM, f"  ({tid})")]
    # Format input dict compactly
    if isinstance(inp, dict):
        for k, v in inp.items():
            sval = repr(v) if not isinstance(v, str) else v
            if len(sval) > max_input_chars:
                sval = sval[:max_input_chars] + "…"
            lines.append(c(C.DIM, f"     {k}: ") + sval.replace("\n", "\n     "))
    else:
        lines.append(f"     {inp!r}")
    return lines


def render_assistant(ev: dict, max_tool_input: int) -> list[str]:
    msg = ev.get("message", {})
    content = msg.get("content", [])
    usage = msg.get("usage", {})
    lines = [
        c(C.BRIGHT_GREEN, "[assistant]")
        + c(C.DIM, f"  in={usage.get('input_tokens', 0)} "
                   f"out={usage.get('output_tokens', 0)} "
                   f"cache_r={usage.get('cache_read_input_tokens', 0):,} "
                   f"cache_w={usage.get('cache_creation_input_tokens', 0):,}")
    ]
    for block in content:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "").rstrip()
            if text:
                for ln in text.splitlines():
                    lines.append(f"  {ln}")
        elif btype == "tool_use":
            lines.extend(render_tool_use(block, max_tool_input))
        elif btype == "thinking":
            thinking = block.get("thinking", "").rstrip()
            if thinking:
                lines.append(c(C.GREY + C.DIM, "  💭 (extended thinking)"))
                for ln in thinking.splitlines()[:10]:
                    lines.append(c(C.GREY, f"     {ln}"))
                if len(thinking.splitlines()) > 10:
                    lines.append(c(C.GREY + C.DIM, "     [... truncated ...]"))
    return lines


def render_tool_result(ev: dict, max_result_chars: int) -> list[str]:
    msg = ev.get("message", {})
    content = msg.get("content", [])
    lines = []
    for block in content:
        if block.get("type") != "tool_result":
            continue
        tid = block.get("tool_use_id", "?")[-6:]
        is_error = block.get("is_error", False)
        icon = "❌" if is_error else "✅"
        color = C.RED if is_error else C.CYAN
        body = block.get("content", "")
        if isinstance(body, list):
            # Sometimes content is a list of blocks
            body = "\n".join(b.get("text", json.dumps(b)) for b in body)
        elif not isinstance(body, str):
            body = json.dumps(body)
        body = body.rstrip()
        header = c(color, f"  {icon} tool_result") + c(C.DIM, f"  ({tid})")
        lines.append(header)
        for ln in truncate(body, max_result_chars).splitlines():
            lines.append(c(C.DIM, "     ") + ln)
    return lines


def render_result(ev: dict) -> list[str]:
    subtype = ev.get("subtype", "")
    dur_ms = ev.get("duration_ms", 0)
    api_ms = ev.get("duration_api_ms", 0)
    turns = ev.get("num_turns", 0)
    cost = ev.get("total_cost_usd", 0.0)
    usage = ev.get("usage", {})
    lines = [
        c(C.BOLD + C.BRIGHT_GREEN if subtype == "success" else C.BOLD + C.RED,
          f"[result/{subtype}]")
        + f"  turns={turns}  duration={format_duration(dur_ms)} "
          f"(api={format_duration(api_ms)})  cost=${cost:.4f}",
        c(C.DIM,
          f"  tokens: in={usage.get('input_tokens', 0):,} "
          f"out={usage.get('output_tokens', 0):,} "
          f"cache_read={usage.get('cache_read_input_tokens', 0):,} "
          f"cache_creation={usage.get('cache_creation_input_tokens', 0):,}"),
    ]
    result_text = ev.get("result", "")
    if result_text:
        lines.append(c(C.DIM, "  ──── result text ────"))
        # Show the last ~1500 chars to catch END_REASON
        for ln in result_text[-1500:].splitlines():
            lines.append(f"  {ln}")
    return lines


# ------------- event classification for --only filter -------------

def classify(ev: dict) -> str:
    t = ev.get("type", "")
    if t == "system":
        return "system"
    if t == "result":
        return "result"
    if t == "assistant":
        msg = ev.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                return "tool_use" if _only_tool_use(ev) else "assistant"
        return "assistant"
    if t == "user":
        msg = ev.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                return "tool_result"
    return t


def _only_tool_use(ev: dict) -> bool:
    """Return True if this assistant event contains only tool_use (no text)."""
    for block in ev.get("message", {}).get("content", []):
        if block.get("type") == "text" and block.get("text", "").strip():
            return False
    return True


# ------------- main -------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pretty-print a Claude Code stream-json NDJSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("--full", action="store_true",
                    help="Do not truncate tool inputs/results")
    ap.add_argument("--compact", action="store_true",
                    help="Extra-tight output (default-ish but hides thinking blocks)")
    ap.add_argument("--only", default=None,
                    help="Comma-sep event classes: system,assistant,tool_use,tool_result,result")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--index", action="store_true",
                    help="Print event-count summary only, no bodies")
    ap.add_argument("--events", default=None,
                    help="Range like '5-20' or '10' to show specific events")
    args = ap.parse_args()

    global _use_color
    _use_color = not args.no_color and sys.stdout.isatty() if not args.no_color else False
    # If piped (not a tty), user can force color back with TERM=xterm-color
    if not args.no_color and "FORCE_COLOR" in __import__("os").environ:
        _use_color = True

    if not args.jsonl.exists():
        print(f"file not found: {args.jsonl}", file=sys.stderr)
        return 1

    max_tool_input = 400 if not args.full else 100_000
    max_result_chars = 1200 if not args.full else 100_000

    only = None
    if args.only:
        only = {s.strip() for s in args.only.split(",")}

    ev_range = None
    if args.events:
        if "-" in args.events:
            lo, hi = args.events.split("-", 1)
            ev_range = (int(lo), int(hi))
        else:
            n = int(args.events)
            ev_range = (n, n)

    events = []
    with open(args.jsonl, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if args.index:
        hist: dict[str, int] = {}
        for ev in events:
            hist[classify(ev)] = hist.get(classify(ev), 0) + 1
        print(f"file: {args.jsonl}")
        print(f"total events: {len(events)}")
        print()
        for k, v in sorted(hist.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>5}  {k}")
        return 0

    for i, ev in enumerate(events, 1):
        cls = classify(ev)
        if only and cls not in only:
            continue
        if ev_range and not (ev_range[0] <= i <= ev_range[1]):
            continue

        t = ev.get("type", "?")
        header = c(C.DIM, f"─── #{i:>4}  {cls:12} ")
        if t == "system":
            print(header)
            print(render_system(ev))
        elif t == "assistant":
            print(header)
            lines = render_assistant(ev, max_tool_input)
            if args.compact:
                lines = [ln for ln in lines if "💭" not in ln]
            print("\n".join(lines))
        elif t == "user":
            print(header)
            lines = render_tool_result(ev, max_result_chars)
            if lines:
                print("\n".join(lines))
        elif t == "result":
            print(rule("final result", C.BOLD + C.BRIGHT_GREEN))
            print("\n".join(render_result(ev)))
        else:
            print(header)
            print(c(C.DIM, f"  [{t}] {json.dumps(ev)[:160]}"))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
