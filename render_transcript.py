#!/usr/bin/env python3
"""Render a run's claude_raw/*.jsonl into readable per-round markdown + an INDEX.

Usage: python3 render_transcript.py <results/<run_id>/<target>> [out_dir]

Drops system/thinking_tokens noise (keeps assistant text, tool_use, tool_result,
result), folds each round's harness gate decision (round_N.json) into the header,
and emits INDEX.md with the run-level summary + a round-by-round table.
"""
import json, subprocess, sys, glob, re
from pathlib import Path

task_dir = Path(sys.argv[1])
out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else task_dir / "TRANSCRIPT_md"
out_dir.mkdir(parents=True, exist_ok=True)
raw = task_dir / "claude_raw"
replay = Path(__file__).parent / "replay.py"

def round_num(p):
    m = re.search(r"round_(\d+)\.jsonl$", str(p))
    return int(m.group(1)) if m else 0

rounds = sorted(raw.glob("round_*.jsonl"), key=round_num)

def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}

result = load(task_dir / "result.json")

# ---- per-round markdown ----
rows = []
for rj in rounds:
    n = round_num(rj)
    gate = load(task_dir / f"round_{n}.json")
    rendered = subprocess.run(
        [sys.executable, str(replay), str(rj), "--no-color",
         "--only", "assistant,tool_use,tool_result,result"],
        capture_output=True, text=True).stdout
    verus = gate.get("verus_okay")
    nerr = len(gate.get("verus_errors") or [])
    er = gate.get("end_reason", "?")
    dur = gate.get("duration_seconds")
    usage = gate.get("claude_usage") or {}
    deleg = gate.get("agent_delegations")
    hdr = [
        f"# Round {n}",
        "",
        f"- **end_reason:** `{er}`",
        f"- **verus_okay:** {verus}  (**{nerr}** error{'s' if nerr != 1 else ''})",
        f"- **duration:** {dur:.0f}s" if isinstance(dur, (int, float)) else "- **duration:** ?",
        f"- **spec_drift:** {gate.get('spec_drift')}",
        f"- **agent_delegations:** {deleg}",
        f"- **usage:** out={usage.get('output_tokens','?')} cache_read={usage.get('cache_read_input_tokens', usage.get('cache_read_tokens','?'))}",
        "",
        "```text",
        rendered.rstrip(),
        "```",
        "",
    ]
    (out_dir / f"round_{n:02d}.md").write_text("\n".join(hdr))
    rows.append((n, er, verus, nerr, dur))

# ---- INDEX.md ----
ac = result.get("admit_classification") or {}
idx = [
    f"# Transcript — {task_dir.parts[-2]} / {task_dir.name}",
    "",
    "## Run summary (result.json)",
    "",
    f"- **end_reason:** `{result.get('end_reason','?')}`",
    f"- **success:** {result.get('success')}",
    f"- **rounds_used:** {result.get('rounds_used','?')}",
    f"- **duration:** {result.get('duration_seconds',0)/60:.1f} min" if result.get('duration_seconds') else "- **duration:** ?",
    f"- **target:** `{result.get('target_path','?')}`",
    f"- **reset_round_starts:** {result.get('reset_round_starts')}",
    "",
    "### Admit classification (final file state)",
    "",
    f"- total **{ac.get('total','?')}**, intentional (axiom_*) **{ac.get('intentional','?')}**, hard **{ac.get('hard','?')}**",
    "",
    "| round | end_reason | verus_okay | errors | dur(s) |",
    "|------:|------------|:----------:|------:|------:|",
]
for n, er, verus, nerr, dur in rows:
    ds = f"{dur:.0f}" if isinstance(dur, (int, float)) else "?"
    idx.append(f"| {n} | {er} | {verus} | {nerr} | {ds} |")
idx += ["", "## Per-round transcripts", ""]
idx += [f"- [Round {n}](round_{n:02d}.md) — `{er}`, verus={verus}, {nerr} err" for n, er, verus, nerr, _ in rows]
(out_dir / "INDEX.md").write_text("\n".join(idx) + "\n")

print(f"wrote {len(rows)} rounds + INDEX.md → {out_dir}")
