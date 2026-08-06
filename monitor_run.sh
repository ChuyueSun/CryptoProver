#!/usr/bin/env bash
# Live monitor of a decompress run: prints every skill invocation, subagent
# launch, and tool call as they stream into the round jsonl.
# Usage: monitor_run.sh <results/<id>/edwards dir> [--once]
set -uo pipefail
R="${1:?results edwards dir}"; ONCE="${2:-}"
PYBIN="${PYTHON:-$(command -v python3 || true)}"
[ -n "$PYBIN" ] || { echo "monitor_run: python3 not found on PATH" >&2; exit 2; }

exec "$PYBIN" - "$R" "$ONCE" <<'PY'
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
once = sys.argv[2] == "--once"
events = []
positions = {}


def record_event(round_number, content):
    name = content.get("name")
    inputs = content.get("input", {})
    if name == "Bash":
        command = inputs.get("command", "") or ""
        match = re.search(
            r"(?:python3?\s+)?skills/(\w+)\.py\s*([^\n|&]*)", command
        )
        if match:
            events.append((
                round_number,
                "🛠  SKILL",
                f"{match.group(1)}.py {match.group(2).strip()[:70]}",
            ))
        elif re.search(r"cargo\s+verus", command):
            events.append((round_number, "🧮 VERUS", "cargo verus verify"))
        else:
            events.append((
                round_number, "📂 bash", command.split("&&")[0].strip()[:70]
            ))
    elif name in ("Agent", "Task"):
        events.append((round_number, "🤝 SUBAGENT", inputs.get("description", "")[:70]))
    elif name in ("Edit", "Write"):
        events.append((round_number, "✏️  " + name, Path(inputs.get("file_path", "")).name))
    elif name == "Read":
        events.append((round_number, "📖 Read", Path(inputs.get("file_path", "")).name))
    else:
        events.append((round_number, "🔧 " + str(name), ""))


def ingest_new_events():
    for path in sorted((root / "claude_raw").glob("round_*.jsonl")):
        match = re.search(r"round_(\d+)", path.name)
        if match is None:
            continue
        stat = path.stat()
        prior_inode, offset = positions.get(path, (stat.st_ino, 0))
        if prior_inode != stat.st_ino or stat.st_size < offset:
            offset = 0
        with path.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            while True:
                line_start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    stream.seek(line_start)
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                for content in event.get("message", {}).get("content", []):
                    if isinstance(content, dict) and content.get("type") == "tool_use":
                        record_event(match.group(1), content)
            positions[path] = (stat.st_ino, stream.tell())


def render():
    skills = Counter(
        event[2].split()[0] for event in events if event[1] == "🛠  SKILL"
    )
    subagents = sum(event[1] == "🤝 SUBAGENT" for event in events)
    print(f"── {len(events)} tool calls │ skills: {dict(skills)} │ subagents: {subagents}")
    for round_number, kind, detail in events[-18:]:
        print(f"  r{round_number} {kind:12} {detail}")


try:
    while True:
        ingest_new_events()
        if not once:
            print("\033[2J\033[H", end="")
            print(f"{time.strftime('%H:%M:%S')} monitoring {root}  (Ctrl-C to stop)")
        render()
        sys.stdout.flush()
        if once:
            break
        time.sleep(5)
except BrokenPipeError:
    # A downstream pager/head exited normally; do not turn that into a traceback.
    try:
        sys.stdout.close()
    except BrokenPipeError:
        pass
PY
