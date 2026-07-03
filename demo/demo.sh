#!/usr/bin/env bash
# Live demo runbook — replays a prior successful run of the Verus proof agent.
# Press Enter between beats. ^C to abort.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGET_DIR="results/layerC_debug_001/straus_lemmas"
JSONL="$TARGET_DIR/claude_raw/round_1.jsonl"
DIFF="$TARGET_DIR/diff.md"

# --- pretty helpers ---
bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; cyan=$'\033[36m'
yellow=$'\033[33m'; reset=$'\033[0m'

beat() {
    clear
    printf '\n%s━━━ BEAT %s ━━━ %s%s\n\n' "$bold$cyan" "$1" "$2" "$reset"
}

pause() {
    printf '\n%s[press Enter to continue]%s ' "$dim" "$reset"
    read -r _
}

# --- pre-flight ---
for f in "$JSONL" "$DIFF" "results/proven_registry.json" "replay.py"; do
    if [ ! -f "$f" ]; then
        printf '%sERROR: missing %s%s\n' "$yellow" "$f" "$reset" >&2
        exit 1
    fi
done

# ===================================================================
beat 0 "SETUP — start the live run in a second terminal"
# ===================================================================
cat <<EOF
  You'll run TWO terminals side by side.

    ${bold}Terminal A (this one):${reset}  ./demo.sh
    ${bold}Terminal B (other):${reset}     ./live_run.sh    ← start this NOW

  Terminal B runs the agent live on one tiny target (elligator_lemmas,
  ~2-3 min) and streams every tool call as it happens — search, Read,
  Edit, verus_check — so the audience sees the actual workflow.
  At Beat 4 we cut to Terminal B for the completed proof and the diff.

  ${dim}(If you don't want a live element, skip Terminal B —
   this demo is fully self-contained without it.)${reset}
EOF
pause

# ===================================================================
beat 1 "The scoreboard — 32 of 35 modules proven"
# ===================================================================
printf '%sSay:%s "This is dalek-lite — real curve25519 cryptography.\n' "$dim" "$reset"
printf '     We pointed an LLM agent at it and asked it to fill in\n'
printf '     the missing proofs. Here is the scoreboard."\n\n'

python3 - <<'PY'
import json
reg = json.load(open('results/proven_registry.json'))['proven']
# For each module, find ANY entry from the original campaign (layer[ABCD]_*).
by = {'A': set(), 'B': set(), 'C': set(), 'D': set()}
for r in reg:
    rid = r['run_id']
    for L in by:
        if rid.startswith(f'layer{L}'):
            by[L].add(r['module'])
            break
print(f'  Layer A (field arithmetic):  {len(by["A"])}/9   verified')
print(f'  Layer B (serialization):     {len(by["B"])}/6   verified')
print(f'  Layer C (Edwards curve):     {len(by["C"])}/15  verified')
print(f'  Layer D (Ristretto):         {len(by["D"])}/5   verified')
print(f'  ─────────────────────────────────────────────')
print(f'  TOTAL:                       {sum(len(v) for v in by.values())}/35  modules')
PY

printf '\n%sPunchline:%s "Not tests. Not fuzzing. Machine-checked Verus\n' "$dim" "$reset"
printf '           proofs — the SMT solver says they are correct."\n'
pause

# ===================================================================
beat 2a "The lemma we're filling in (what it means)"
# ===================================================================
cat <<EOF
${dim}Say:${reset} "Let's zoom into ONE of the 26 lemmas the agent proved in
     this file. Plain English first."

  ${bold}lemma_select_is_signed_scalar_mul_projective${reset}

  The cryptography:
    Ed25519 signature verification computes ${bold}x · B${reset} (a scalar times
    a curve point) thousands of times. To stay constant-time, dalek
    precomputes a small table ${bold}T = [B, 2B, 3B, ..., 8B]${reset} and looks up
    multiples by signed index x ∈ {-8..+8}.

  What this lemma says:
    "If x > 0, T[x-1] gives x·B.
     If x = 0, the identity point gives 0·B.
     If x < 0, negating T[-x-1] gives x·B."

  Why we care:
    This lookup runs on every bit of every scalar in every Ed25519
    verification. If it's wrong, signatures fail — or worse, can be
    forged. Proving it correct is a ${bold}real${reset} security goal, not a toy.

EOF
pause

# ===================================================================
beat 2b "The proof the agent wrote"
# ===================================================================
printf '%sBefore:%s\n\n' "$dim" "$reset"
cat <<'EOF'
  pub proof fn lemma_select_is_signed_scalar_mul_projective(...)
      ensures
          projective_niels_point_as_affine_edwards(result)
              == edwards_scalar_mul_signed(basepoint, x as int),
  {
      admit()        ← the cheat. SMT accepts ANY postcondition here.
  }
EOF

printf '\n%sAfter (what the agent wrote):%s\n' "$dim" "$reset"
# Show ONLY lemma_select_is_signed_scalar_mul_projective from the diff
awk '
  /^@@ -193,7/ {found=1}
  found && /^@@ -233,7/ {exit}
  found {print}
' "$DIFF"
pause

# ===================================================================
beat 2c "How each line was found"
# ===================================================================
cat <<EOF
${dim}Say:${reset} "Three reasoning primitives — that's it."

  ${bold}1.  The case split (if/else if/else on sign of x)${reset}
      Came from reading the postcondition. The spec literally enumerates
      three cases on x's sign, so the proof mirrors them.

  ${bold}2.  Two ${cyan}library lemma${reset}${bold} calls${reset}
      ${cyan}lemma_identity_projective_niels_is_identity()${reset}
      ${cyan}lemma_negate_projective_niels_is_edwards_neg(inner)${reset}
        → Both came from the project's own lemma library. The agent
          first dispatched an Explore ${bold}subagent${reset} to map
          straus_lemmas's dependencies, then used the discovered
          lemmas verbatim. (For other lemmas in this file, it
          searched directly with search_semantic / search_module.)

  ${bold}3.  ${yellow}assert(...)${reset}${bold} hints to the SMT solver${reset}
      assert(0 <= j < 8);             ← bound check, trivial for SMT
      assert((j + 1) as nat == x as nat);  ← arithmetic identity
      These don't ${bold}prove${reset} anything — they tell Verus what intermediate
      fact to chase. Without them, Verus times out. With them, it
      finds the connecting arithmetic in milliseconds.

${dim}Punchline:${reset} "The agent invented zero math. It read the spec, picked
           an obvious case split, searched the library twice, and
           sprinkled SMT hints. Then verus_check said OK."
EOF
pause

# ===================================================================
beat 3 "The agent at work — one module, 20 minutes"
# ===================================================================
printf '%sSay:%s "What did the agent actually do? Here is the receipt."\n\n' "$dim" "$reset"

python3 replay.py "$JSONL" --index --no-color

printf '\n%s──── tool-use breakdown ────%s\n\n' "$dim" "$reset"

python3 - "$JSONL" <<'PY'
import sys, json, re, collections
events = []
for l in open(sys.argv[1]):
    l = l.strip()
    if not l: continue
    try: events.append(json.loads(l))
    except: pass
c = collections.Counter()
for ev in events:
    if ev.get('type') != 'assistant': continue
    for b in ev.get('message', {}).get('content', []):
        if b.get('type') != 'tool_use': continue
        if b['name'] == 'Bash':
            cmd = b.get('input',{}).get('command','')
            m = re.search(r'skills/(\w+)\.py', cmd)
            if m: c[f"skills/{m.group(1)}.py"] += 1
            elif 'verus' in cmd: c['cargo verus  (SMT verify)'] += 1
            else: c['bash (read/grep/ls)'] += 1
        else:
            c[b['name']] += 1
for k,n in c.most_common(): print(f"  {n:>3}x  {k}")
n_smt    = sum(v for k,v in c.items() if 'verus' in k.lower())
n_edit   = c.get('Edit', 0) + c.get('Write', 0)
n_search = sum(v for k,v in c.items() if 'search_' in k)
print()
print(f"\033[2mNarrate:\033[0m \"{n_smt} SMT checks. {n_edit} proof rewrites. {n_search} library")
print(f"         searches. Plans its own work with TodoWrite. The same")
print(f"         loop a human does: search → propose → verify → iterate.\"")
PY
pause

# ===================================================================
beat 3.5 "The bill"
# ===================================================================
printf '\n'
python3 replay.py "$JSONL" --only result --no-color | grep -E "^\[result|tokens" | head -2
pause

# ===================================================================
beat 4 "Takeaway"
# ===================================================================
clear
cat <<'EOF'

  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  THREE THINGS:                                               │
  │                                                              │
  │   1. The whole orchestrator is ~1000 lines of Python.        │
  │      No framework, no harness magic. Read it in an hour.     │
  │                                                              │
  │   2. We do not trust the LLM. We trust the SMT solver.       │
  │      Every claim is machine-checked.                         │
  │                                                              │
  │   3. ~$10 per module × 32 modules ≈ $300 to formally         │
  │      verify a meaningful chunk of a production crypto crate. │
  │      The price of an afternoon.                              │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘

EOF
printf '\n%sNow cut to Terminal B — the live run should be done by now,%s\n' "$dim" "$reset"
printf '%sshowing the actual proof the agent just wrote on this machine.%s\n\n' "$dim" "$reset"
printf '%s(end of demo)%s\n\n' "$dim" "$reset"
