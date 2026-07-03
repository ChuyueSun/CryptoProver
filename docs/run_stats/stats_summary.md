# Field-floor run stats (session 2026-06-28)

Experiment: Verus proof reconstruction of the curve25519-dalek field-floor cut
(target `ristretto.rs`, reconstruct the whole above-field proof cone, ~26 editable
files). Scored by **whole-crate verus errors → 0 AND non-axiom admits → 0**.

> **Reading the numbers.** A low whole-crate *error* count is NOT "near complete":
> `admit()` placeholders trivially satisfy obligations, so a run can show few
> errors while leaving many proofs unproven. Always read **errors AND
> hard-admits-remaining together**. Also, transient low error counts appear when a
> mid-edit whole-crate verify fails to *compile* (reports only compile errors) —
> use the **final/settled** value + `result.json`, not the series minimum.

## Completed runs

| Run | Type | end_reason | rounds | whole-crate (final) | hard admits | cost | notes |
|-----|------|-----------|:-:|--|:-:|--|--|
| **gcp14** | bare-metal clean (control) | LIMIT | 4 | **214** strict / **203** rlimit-gate | **0** | $53.33 | honest 214 unproven, no admits; dur 11132.043707s |
| **ff_docker_resume_001** | docker resume, seeded + tapped | SIBLING_VERUS_FAIL | 1 | probe **2** (settled) | **66** | n/a* (25k out-tok) | canonical `final_error_count=4`; NOT near-complete (66 deferred lemma proofs) |

_*`n/a` cost = the round was SIGKILLed at its budget deadline before the `result` event that records cost; output-token count (from `assistant_usage_event_sums`) shown instead._

**resume001 error trajectory:** 173 → 171 → 67 → 3 → 2 (whole-crate errors), reached
by deferring **66 lemma proofs as `admit()`** (curve-equation/group-law, montgomery
reduce, straus/pippenger, niels). Real position ≈ 2 errors + 66 admitted ≈ 68
unresolved obligations — still better than gcp14's 214, but not a complete proof.

## Incomplete / non-scored
- **ff_docker_resume_002** — live continuation, seeded from run001's 2-error/66-admit
  state; no sealed result at archive time (target-module checks 6→3→2; no whole-crate yet).
- **ff_docker_clean_001** — docker clean (untapped); no sealed result; whole-crate probe
  ~127 seen; superseded/killed in the drop-to-2-agents step.
- **ff_docker_resume_001 (1st untapped)** — superseded in cleanup; only scalar-module checks.

## Other lane (not field-floor)
- **VM2 trusted-core** (`trustedcore_resume_007`, codex's lane) — `ORACLE_LEAK` terminal
  (round 19, rc=-9): agent read proven source → **score VOIDED** (~290 invalid).

## Infrastructure delivered this session
- `f3bfa28`: PreToolUse verifier hook + `detect_process_crosstalk` hardening — pushed to
  `main`, deployed, hook validated live (0 PROCESS_CROSSTALK across every field-floor run).
- r4 docker image `dalek-harness:f3bfa28`: hook + `--seed-wip` (docker resume) + claude-tap
  wiring + `search_semantic` ANTHROPIC_BASE_URL strip; codex-reviewed, preflight-green.

## Archive contents (off-VM, durable)
- `raw/` — full run dirs (result.json, round_*.json + raw_usage_summary, cli.log,
  spec_snapshot.json, prompt_rendered.md, claude_raw/*.jsonl), patches, launcher/container logs.
- `raw/claude_tap_traces.sqlite3` — claude-tap trace DB (92 MB; local-copy counts:
  8 sessions / 1185 records /
  2387 blobs / 2389 proxy_logs).
  **Local only — not committed to repo.**
- `stats_index.json` — machine-readable per-run metrics + full whole-crate series + token/cost usage
  (parsed from each round's `claude_usage`).

_Canonical numbers cross-checked with codex (AGENT_DEBATE 06:01/06:05)._
