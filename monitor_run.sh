#!/usr/bin/env bash
# Live monitor of a decompress run: prints every skill invocation, subagent
# launch, and tool call as they stream into the round jsonl.
# Usage: monitor_run.sh <results/<id>/edwards dir> [--once]
set -uo pipefail
R="${1:?results edwards dir}"; ONCE="${2:-}"
PYBIN=/path/to/python3/bin/python3
watch_once() {
"$PYBIN" - "$R" <<'PY'
import json,sys,glob,re,os
R=sys.argv[1]
events=[]
for f in sorted(glob.glob(os.path.join(R,"claude_raw","round_*.jsonl"))):
    rnd=re.search(r"round_(\d+)",f).group(1)
    for line in open(f):
        try: e=json.loads(line)
        except: continue
        if e.get("type")!="assistant": continue
        for c in e.get("message",{}).get("content",[]):
            if not isinstance(c,dict) or c.get("type")!="tool_use": continue
            n=c.get("name"); inp=c.get("input",{})
            if n=="Bash":
                cmd=inp.get("command","") or ""
                m=re.search(r"(?:python3?\s+)?skills/(\w+)\.py\s*([^\n|&]*)",cmd)
                if m: events.append((rnd,"🛠  SKILL",f"{m.group(1)}.py {m.group(2).strip()[:70]}"))
                elif re.search(r"cargo\s+verus",cmd): events.append((rnd,"🧮 VERUS","cargo verus verify"))
                else: events.append((rnd,"📂 bash",cmd.split('&&')[0].strip()[:70]))
            elif n in ("Agent","Task"):
                events.append((rnd,"🤝 SUBAGENT",inp.get("description","")[:70]))
            elif n in ("Edit","Write"): events.append((rnd,"✏️  "+n,(inp.get("file_path","")).split("/")[-1]))
            elif n=="Read": events.append((rnd,"📖 Read",(inp.get("file_path","")).split("/")[-1]))
            else: events.append((rnd,"🔧 "+n,""))
# summary
from collections import Counter
skills=Counter(e[2].split()[0] for e in events if e[1]=="🛠  SKILL")
agents=[e[2] for e in events if e[1]=="🤝 SUBAGENT"]
print(f"── {len(events)} tool calls │ skills: {dict(skills)} │ subagents: {len(agents)}")
for rnd,kind,detail in events[-18:]:
    print(f"  r{rnd} {kind:12} {detail}")
PY
}
if [ "$ONCE" = "--once" ]; then watch_once; exit 0; fi
echo "monitoring $R  (Ctrl-C to stop)…"
while true; do clear 2>/dev/null; date '+%H:%M:%S'; watch_once; sleep 5; done
