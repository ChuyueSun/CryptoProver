# Harness size — LOC by trust-model role

Counting method: `wc -l` per file; attribution inside `run.py` by
top-level `def`/`class` extents. **Measured at CryptoProver commit
`787356d`** (the public tree carrying both dual-signed mid-arm fixes;
re-run the same method at any pinned commit to reproduce — the mvp
research tree carries additional uncommitted work and will not match).
Dual-use files are counted **once**, in the bucket whose harness-side
role is authoritative (rule below). Comments and blanks included.

## Arithmetic (read this first)

**Agent runtime** = the runtime code groups that execute during an
attempt: `run.py` 6,585 + the nine executable `skills/*.py` tools 2,661
(the `skills/` tree also holds 398 lines of skill docs — SKILL.md and
`infer_verus_spec/` — not counted as code) + `lib/` 1,630 =
**10,876 LOC**. The
buckets below attribute it as: prover ≈ 3,479; referee ≈ 2,442;
task-definition code that lives inside `run.py` (experiment-mode blocks)
≈ 970; and ≈ 3,985 of `run.py` runtime helpers not individually
attributed (subprocess/deadline/process-group management, usage
accounting, spec-drift restore, admit classification wiring, ~50 small
functions) — some of that remainder is gate wiring, so the referee share
is a **lower bound**. Referee / runtime = 2,442 / 10,876 ≈ 22%
("roughly a quarter"). Bench-outside-runtime and ops are separate
non-runtime code and are NOT in the 10.9k denominator.

## The four buckets

**1. Prover (the "agent") — ~3.5k Python + 412 lines of prompt.**
An off-the-shelf LLM, `prompt.md` (412 lines, data not code), the tools
the agent calls in a round, and a thin driver loop. No planner, no
cascade, no proof-search algorithm.

| piece | LOC |
|---|---|
| `skills/verus_check.py` (agent's feedback signal; harness re-runs it independently) | 523 |
| search stack (`lib/catalog.py` 435 + 4 search skills 530) | 965 |
| `lib/failure_memory.py` / `lib/discovery_brief.py` / `lib/results.py` | 193 / 243 / 125 |
| `skills/diff_view.py` | 230 |
| driver loop in `run.py` (claude subprocess runner + prompt rendering + round feedback: `run_claude_round`, `render_prompt`, `build_round_history_block`, `build_failure_queue_block`) | ~700 |
| `run_task` share attributable to plain round sequencing | ~500 |

**2. Referee (integrity gates) — ~2.4k Python.** Adversarial to the
prover; exists because the agent's incentive is to make verification
pass, not to prove things.

| piece | LOC |
|---|---|
| `skills/spec_check.py` (spec-integrity gate incl. `--check-spec-defs`) | 765 |
| `lib/admits.py` + `skills/admit_inventory.py` (admit/axiom/forbidden-construct counters) | 495 |
| `skills/check_false_contract.py` (contract-hollowness) | 530 |
| `lib/verifier_policy_hook.py` (redirect/background-verifier blocking) | 222 |
| end-reason/promotion gate + tooling-hash + exit classification in `run.py` (`_final_end_reason`, `_classify_claude_no_result_exit`, snapshot/diff helpers) | ~430 |

**3. Bench (task construction) — ~3.5k.** Builds the graded cuts;
never runs during an attempt: `peel.py` 769, `strip_specs.py` 787,
`admit.py` 304, experiment-mode blocks inside `run.py` ~970 (the one
bench piece that lives in a runtime file), demo/rung scripts ~680.

**4. Ops (evaluation infrastructure) — ~4.2k shell/Python.** Docker
per-agent isolation (~1.8k shell + Dockerfile), launchers/sweep-resume
(`launch.sh`, `peel_run.sh`, `run_layer.py`, …) ~1.3k, `replay.py` 337,
misc.

Plus **tests: ~6.2k** (pinning the gate/counter/peel logic — the
referee's own referee).

## Dual-use rule

`verus_check` and `spec_check` are both agent-callable tools and harness
scoring instruments. Classification is by whose invocation is
authoritative: the harness re-runs both as subprocesses and only those
runs decide `end_reason`, but `verus_check`'s primary role is the
agent's feedback loop (counted: prover), while `spec_check`'s primary
role is the drift gate (counted: referee). Moving `verus_check` to the
referee bucket shifts ~0.5k between buckets and changes no conclusion.

## The headline

Agent runtime ≈ 10.9k LOC, of which ≈ 2.4k (22%, a lower bound — see
Arithmetic) is integrity gating: **roughly a quarter of the runtime code
exists to distrust the rest.** The prover-proper is a 412-line prompt,
nine small CLI tools, and a single readable driver loop — the capability
comes from the model; the engineering contribution is the gating that
makes an untrusted prover's output certifiable, plus the peel builder
that makes the evaluation graded and reproducible.
