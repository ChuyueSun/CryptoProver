---
name: dalek-lite-mvp
description: "Exact flags, invocation syntax, and tactical notes for the dalek-lite proof CLIs: verus_check, spec_check, admit_inventory, search_semantic/module/macro/proven."
---

# Dalek-Lite MVP — Skills

CLIs the proof agent invokes via Bash. Each returns JSON on stdout
(see per-skill notes for the exceptions) and logs to `$CLI_LOG_PATH`
(set by `run.py` per task).

| Skill | Purpose | First thing to know |
|---|---|---|
| `verus_check.py` | Run `cargo verus --verify-module` | Source of truth for "did it verify" |
| `spec_check.py` | Snapshot / verify signature integrity | Agent edits that weaken specs fail the round |
| `admit_inventory.py` | Count actionable vs axiom admits and list their line numbers | `non_axiom_count == 0` is the COMPLETE condition; comments and `axiom_*` bodies are filtered out |
| `search_semantic.py` | Keyword/substring search over catalog (incl. vstd) | First try for "I'm looking for something about X" |
| `search_module.py` | List all sigs from one module (incl. vstd modules) | Use after seeing `use crate::...` or `use vstd::...` |
| `search_macro.py` | Expand `lemma_*!` macro families | Use when semantic search misses generated lemmas |
| `search_proven.py` | Query ProvenRegistry | Check if a lemma was proven earlier in the campaign |
| `diff_view.py` | Render admitted→final→truth diff as markdown | Diagnostic, not agent-facing; emits markdown, not JSON |

The `infer_verus_spec/` directory is a documentation-only skill (no CLI) —
guidance for reconstructing stripped fn-header specs.

`strip_specs.py` is NOT here — it is a top-level init/harness tool (a sibling of
`run.py` / `admit.py`) that builds an experiment's stripped starting state. The
proof agent never invokes it during a round, so it does not belong in `skills/`.

`check_false_contract.py` is harness-private even though it lives under
`skills/` for standalone debugging. The proof agent writes
`false_contract_claims.json`; `run.py` invokes this checker against the frozen
snapshot. It is intentionally not part of the agent-facing table below.

---

## Detailed reference (agent-facing skills)

**This section is the single source of truth for skill flags and examples.**
`prompt.md` carries only a one-line index and points here; keep the two in sync
(index terse, detail here). Each file also has `-h`.

In the commands below, substitute the concrete per-run paths printed in
`prompt.md`'s **## Target** block for the `<UPPER_CASE>` tokens — e.g. plug the
real catalog-cache path in for `<CATALOG_CACHE>`, and append the `<VSTD_FLAG>`
value (` --vstd-root …`, or nothing if vstd isn't indexed) to `search_*` calls.
The rendered prompt also gives a concrete `<SKILLS_DIR>` path. Use that absolute
path for every skill invocation. The harness starts each Bash tool call in the
Cargo project root, and that project root does not contain a `skills/`
directory.

Run verifier skills in the foreground and consume their JSON stdout directly.
Do not start background `verus_check` / `cargo verus` jobs, do not redirect
verifier JSON to fixed shared files like `/tmp/vcheck.json`, and do not use
broad process controls such as `pkill`, `killall`, or `pgrep -f`. The harness
owns timeouts and process cleanup; shared process/tmp controls can corrupt
concurrent runs. Do not wrap verifier commands in shell `timeout` (not
portable; it fails on macOS). Use `verus_check.py --timeout N` instead. Do not
substitute direct `cargo-verus focus`, raw `cargo verus`, or `cargo build`
greps for verifier truth; direct cargo-verus can forward module filters into
vstd/dependency crates and report misleading `available modules are:
arithmetic...` errors. Use `verus_check.py` for module and whole-crate checks.
Do not merge stderr into a JSON parser (`2>&1 | python -c 'json.load(...)'`); parse
stdout alone, and inspect stderr separately only after a command fails.
Do not `sleep N; cat .../tasks/*.output` to wait for hidden task files; those
waits are blocked by the shell tool. If you did not run a check in the
foreground, rerun the needed verifier/search command in the foreground instead.

Prefer `rg -n PATTERN src -g '*.rs'` for source searches. The shell may treat
unmatched unquoted globs as fatal, so avoid `grep --include=*.rs`; if you must
use grep/ls/find globs, quote them (`--include='*.rs'`, `'*.sh'`) or use `find`.
Do not assume remembered dalek file paths from docs or prior runs exist in this
peeled worktree. Before opening a non-target support file, confirm the current
layout with `rg --files src` / `find`, or search by symbol and use the returned
path.
Root source discovery at the current Cargo project: use `rg --files src`,
`find src ...`, explicit editable files, or documented per-run paths. Do not
use global filesystem searches (`find /`, broad `/tmp` or
`/home/.../dalek-peel` scans) to locate source; ignore candidate source paths
outside the current project/results/vstd roots unless the prompt explicitly
named them.
When deleting or moving Rust/Verus items, delete or move attached `///` doc
comments and `#[...]` attributes with the item. Orphaned docs/attributes cause
parser errors such as `unexpected token, expected ;` or `unexpected end of
input`; after item-deletion edits, do a cheap syntax/source check before deeper
proof work.

### Verification

- `python3 <SKILLS_DIR>/verus_check.py <TARGET> --project <PROJECT_ROOT>`
  Runs `cargo verus --verify-module` on the target module. `<TARGET>` is an
  absolute `.rs` file path such as
  `<PROJECT_ROOT>/src/lemmas/edwards_lemmas/niels_addition_correctness.rs`
  (project-relative `.rs` paths also work). It is not a crate name like
  `curve25519-dalek` and not a module name like
  `lemmas::edwards_lemmas::niels_addition_correctness`. JSON with
  `okay`, `error_count`, `summary` (errors grouped by file/line — read this
  first), `messages[]`, `errors[]` (compatibility alias for `messages[]`),
  `message_texts[]`, `error_texts[]`, `failed_declarations[]`. `messages[]`
  and `errors[]` are structured diagnostic objects; use `summary`,
  `message_texts[]`, or `error_texts[]` when you want strings to print or
  slice. Module checks are frequent and **fast — call often**.

  **`--whole-crate`** verifies the ENTIRE package (no `--verify-module`) and
  returns the SAME structured `summary`/`messages[]`. It is **slow (~590s) but
  authoritative** (default timeout 900s). Read `summary`/`messages[]` and do not
  re-run whole-crate just to re-slice its output. Do **not** substitute `cargo
  verus verify … | grep | head` yourself: that truncates the error set (you'll
  miss earlier errors, conclude "fixed" wrongly, then waste a full re-verify).
  `summary`/`messages[]` are the complete source of truth; `stderr_tail` is
  truncated raw context only. If a whole-crate check is already running, wait
  for its JSON before launching any other Verus check; do read-only source or
  search work meanwhile.
  In whole-crate mode only, the `<TARGET>` positional may be omitted when
  `--project <PROJECT_ROOT>` is supplied; the skill uses the project root's
  `src/lib.rs` as the diagnostic anchor.

  **`--timeout SECONDS`** changes the skill's portable verifier wall-clock
  budget. Use this flag rather than shell `timeout`; the skill kills the full
  cargo/verus/z3 process group on expiry.

  **`--rlimit FLOAT`** is forwarded to verus (SMT resource limit in
  roughly-seconds). Default is verus's built-in (~10). For long exec-mode
  functions where verus hits a per-fn rlimit before your proof gets feedback,
  bump it: `--rlimit 80` (or higher). If you see `"resource limit exceeded"` in
  the error messages, this is the first lever to try — much cheaper than
  restructuring the proof.

- `python3 <SKILLS_DIR>/spec_check.py verify <TARGET> --against <SPEC_SNAPSHOT>`
  Detect whether you've modified any original spec. Call before declaring
  COMPLETE. `--against` is the canonical explicit form; harness-launched rounds
  also set `$SPEC_SNAPSHOT`, which `verify` uses as a fallback if you omit
  `--against`. A stray `--project <PROJECT_ROOT>` is accepted but ignored for
  compatibility. The JSON always preserves raw audit evidence in `drift`.
  Generated / reconstructed `lemma_*` contract repairs under `lemmas/` are
  reported separately as `allowed_generated_contract_drift`; use
  `blocking_drift` and `okay` for the agent-facing decision. The harness still
  applies its own authoritative allow-edit gate to raw `drift`. (The `snapshot`
  and `list-siblings` subcommands are harness-driven.)

- `python3 <SKILLS_DIR>/admit_inventory.py <TARGET> [--siblings <helper.rs> ...]`
  Count actionable admits in the target (and any sibling helpers you changed).
  Returns `non_axiom_count`, `axiom_count`, plus per-line entries.
  `non_axiom_count == 0` is the COMPLETE condition. Comments and
  `proof fn axiom_*` bodies are filtered out automatically. Pass `--siblings`
  for any helper files you added so their non-axiom admits are counted too.

### Search (use aggressively when you need a lemma)

**The catalog indexes BOTH the project source AND vstd.** You can query vstd
modules (e.g. `vstd::arithmetic::mul`, `vstd::arithmetic::power2`, `vstd::bits`)
directly — no need to grep the vstd tree manually for lemma *names*.

- `python3 <SKILLS_DIR>/search_semantic.py "<natural language query>" --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG> -n 5`
  First thing to try when you don't know the exact name. Returns hits from BOTH
  dalek-lite source AND vstd. Examples:
  - `"pow2 adds and multiplies"` → finds `lemma_pow2_adds` in `vstd::arithmetic::power2`
  - `"distributive multiplication"` → finds `lemma_mul_is_distributive_add` in `vstd::arithmetic::mul`
  - `"field element bounded by prime"` → finds dalek-lite local lemmas

- `python3 <SKILLS_DIR>/search_module.py "<module>" --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG>`
  List every public signature in one module. Works for both project and vstd
  modules. Examples:
  - `"crate::lemmas::common_lemmas::pow_lemmas"` — pre-built dalek lemmas
  - `"vstd::arithmetic::mul"` — multiplication lemmas + broadcast groups
  - `"vstd::arithmetic::power2"` — pow2 lemmas
  Use after spotting a `use crate::foo::bar::*` or `use vstd::...` line.

- `python3 <SKILLS_DIR>/search_macro.py --name-prefix lemma_u8_pow2 --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG>`
  Many lemmas are generated by `lemma_*!(NAME, TYPE)` macro invocations and
  won't show up via grep of the source. This skill exposes those. Use when
  semantic search didn't find what you expected.

**Tactical-use note**: `search_*` returns signatures only — not doc comments or
proof bodies. If you need to see HOW a lemma is used or read its `///` docstring
for context, fall back to `Read` on the file or `grep -B2` for nearby context.

- `python3 <SKILLS_DIR>/search_proven.py --results <RESULTS_ROOT> --name lemma_foo`
  Check whether a lemma you're about to call was proven earlier in this
  campaign. Prefer verified-in-registry lemmas over unverified ones.
