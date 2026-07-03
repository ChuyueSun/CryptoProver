# Peel manifests — the proof-gen experiment rungs, as data

Each file here is a **peel manifest**: a declarative description of one
proof-reconstruction init state, consumed by [`peel.py`](../peel.py) (to build
the worktree) and [`peel_run.sh`](../peel_run.sh) (to launch `run.py` on it).
They are the canonical reconciliation of the old `--experiment-mode` rungs
(previously hard-coded in `demo_decompress.sh` / `launch_specgen.sh`) into the
single peel-depth axis.

A manifest answers four questions:

| key | meaning |
|-----|---------|
| `depth` | how many proof-stack shells to peel (P1 proofs → P2 lemmas → P3 specs → P4 contract). `peel_run.sh` uses it when `--depth` is omitted. |
| `experiment_mode` | the run-side mode `peel.py` echoes into its JSON → `run.py --experiment-mode`. **Declared explicitly, never inferred from depth** (see the long NOTE in `peel.py` — depth cannot express "strip stratum k, keep 1..k-1 as the pin"). |
| `pin` | the soundness pin. Required by `peel.py` for P4 (contract strip) and any P3 that deletes a `spec fn`; recorded for the others. `proof` \| `consumer:NAME` \| `oracle:REF`. |
| `target` | the `run.py` anchor under test, relative to the worktree root. `peel_run.sh` needs it; `peel.py` ignores it. |
| `files[]` | per-file ops: `proof_op` (`admit`/`strip`/`strip-all`/`none`), `strip_proof_fns`, `lemmas`, `spec_fns`, `contract_fns`. Every listed file is **editable**; the run's frozen guard = everything *not* listed. |

## The hardest setup: the generated field-floor cut

Before the curated rungs below — **the single hardest peel setup is the
*generated* field-floor cut**, not any hand-written manifest. It freezes only the
L4/L5 core (field lemmas, the number-theory/`common_lemmas` substrate, every
`specs/*`, the backend, all `axiom_*`) and peels the **entire** proof cone above
it: every above-field correctness lemma is deleted and all four API proof bodies
(`edwards.rs`, `montgomery.rs`, `ristretto.rs`, `scalar.rs`) are red-stripped.

Generate it, then add the run metadata:

```bash
python3 peel.py --classify /path/to/wt/curve25519-dalek > peel_manifests/field_floor.json
# then add to the JSON:  "depth": 2, "experiment_mode": "field-floor",
#                        "pin": "proof", "target": "curve25519-dalek/src/ristretto.rs"
./peel_run.sh --manifest peel_manifests/field_floor.json --surface   # ~26 editable, 235 lemmas
```

**It is still only peel depth 2** (proofs + lemmas; specs + contract frozen) —
the same depth as the decompress rungs. The difficulty is **cone breadth, not
depth**: `--classify` makes "lemmas" mean the whole above-field cone (235 lemmas /
26 editable files) instead of the ~10-name decompression path. Soundness is
identical to the curated rungs (`pin: proof` holds for the whole cone at once).
See the CryptoProver paper for the breadth-not-depth argument and the
`peel_corefloor_001` run. A committed
`field_floor.json` (classified from the proven tip `103b92b9`) lives alongside the
curated manifests here.

## The deeper floors: `number-theory` and `trusted-core`

`--classify` takes a `--classify-floor` (`_CLASSIFY_FLOORS` in `peel.py`):
`field` (above) is the shallowest; **`number-theory`** also deletes the field
lemmas and strips the backend field-exec proofs; **`trusted-core`** strips every
in-repo non-axiom proof artifact, freezing only external `vstd` + every
`axiom_*`. The three are a **monotone superset ladder** — each harder floor cuts
a strict superset (enforced by `_merge_manifest_entries`). All three are still
**peel depth 2** (proofs + lemmas; *every* `spec fn`, contract, and `axiom_*`
frozen → `del_specs = 0`, `strip_contract = 0`), so `pin: proof` is the correct
and sufficient pin for all three — the difficulty is **cone breadth, not depth**:

| floor | `--classify-floor` | editable files | lemmas deleted | depth | pin |
|-------|--------------------|:--:|:--:|:--:|------|
| field         | `field` (default) | 26 | 235 | 2 | proof |
| number-theory | `number-theory`   | 50 | 504 | 2 | proof |
| trusted-core  | `trusted-core`    | 86 | 815 | 2 | proof |

(Counts from the proven tip `103b92b9`, 2026-06-25. The trusted-core 86th file
is `backend/serial/curve_models/mod.rs`: a `mod.rs` with real inline
`proof {}`/`assert` scaffolding, so `strip-all` peels it. The classifier
content-gates `mod.rs`/`axioms.rs` — it skips them only when they hold no
*removable* proof content, not by name — so true-glue `mod.rs` and pure-axiom
files stay frozen while a proof-bearing `mod.rs` is editable. Lemma count is
unchanged at 815: curve_models contributes inline-proof strips, not deleted
lemmas.) There is **no
`--experiment-mode number-theory-floor`**: `run.py` only accepts `field-floor`
and detects the deeper variant from the *editable file set*
(`run.py:2569`/`2574` — `field_layer_editable` / `trusted_core_editable`). So
every floor's manifest declares `experiment_mode: "field-floor"`; the `name`/
`floor` fields are for humans, not the dispatcher.

### Reproduce (coauthor-runnable, this machine)

```bash
cd /path/to/cryptoprover
# toolchain on PATH (same dirs peel_run.sh/demo_pool_setup.sh prepend):
export PATH="/path/to/python3/bin:/tmp/verus-rel/verus-arm64-macos:/path/to/.local/bin:$PATH"

REPO=/tmp/dalek-baf                 # the dedicated dalek clone (DALEK_SRCREPO default)
REF=corefloor-base-103b92b9         # the PROVEN tip — NOT `main` (main is unborn in this clone)

# 1) classify from a CLEAN proven tree (never a half-peeled/dirty worktree).
#    Cut a throwaway worktree at the proven ref so the primary checkout is untouched:
git -C "$REPO" worktree add --detach /tmp/dalek-classify "$REF"
PROJECT=/tmp/dalek-classify/curve25519-dalek

# 2) generate each manifest (strip peel's okay/project keys, add run metadata).
for FLOOR in number-theory trusted-core; do
  OUT="peel_manifests/${FLOOR/-/_}_floor.json"          # → number_theory_floor.json / trusted_core_floor.json
  python3 peel.py --classify "$PROJECT" --classify-floor "$FLOOR" \
  | python3 -c 'import json,sys
m=json.load(sys.stdin); m.pop("okay",None); m.pop("project",None)
out={k:m[k] for k in ("name","floor","files") if k in m}
out.update({"depth":2,"experiment_mode":"field-floor","pin":"proof",
            "target":"curve25519-dalek/src/ristretto.rs"})
print(json.dumps(out, indent=2))' > "$OUT"
done

git -C "$REPO" worktree remove --force /tmp/dalek-classify   # done classifying
```

### Surface (no worktree, no build, no tap)

```bash
./peel_run.sh --manifest peel_manifests/number_theory_floor.json --surface
./peel_run.sh --manifest peel_manifests/trusted_core_floor.json  --surface
```

### Run

`peel_run.sh` already prepends this machine's toolchain and defaults
`DALEK_SRCREPO=/private/tmp/dalek-baf`, so you only pass the proven `--ref`,
rounds/budget, and `--detach`. **claude-tap is ON by default** (each run becomes
a browsable session at the `:8799` dashboard); headless auth comes from the
keychain login or `CLAUDE_CODE_OAUTH_TOKEN`.

```bash
./peel_run.sh --manifest peel_manifests/number_theory_floor.json \
    --run-id nt_floor_001 --ref corefloor-base-103b92b9 \
    --rounds 20 --budget 360 --model opus --detach

./peel_run.sh --manifest peel_manifests/trusted_core_floor.json \
    --run-id trusted_core_001 --ref corefloor-base-103b92b9 \
    --rounds 30 --budget 480 --model opus --detach
```

> **tap port collision — check before launching.** The default tap port is
> `58960`. Launching a peel run *bounces* (restarts) the proxy on that port,
> which **kills the session any in-flight peel run is attached to**. Before you
> launch, confirm nothing is mid-run: `pgrep -fl 'run.py|claude -p'`. If a run
> is live and you want a *concurrent* one, give the new run its own proxy:
> `DALEK_TAP_PORT=58961 DALEK_TAP_LIVE_PORT=8800 ./peel_run.sh …`, or disable
> capture for it with `--no-tap`. A separate run also needs its own
> `DALEK_RESULTS` to avoid the cumulative-JSON race (see CLAUDE.md).

## The five canonical decompress rungs

These reproduce the frozen-contract rungs of `demo_decompress.sh`, in increasing
difficulty — the hardest *hand-written* manifests (the field-floor cut above is
the harder generated superset). (The two gate-OFF contract-reconstruction rungs —
`no-spec`, `no-lemmas` — are *not* mirrored here; see "What's not here" below.)

| Manifest | depth | mode | old `demo_decompress.sh` rung | what the agent rebuilds |
|----------|:---:|------|------------------------------|--------------------------|
| `decompress_proof_only.json` | 1 | `proof-only` | `--formal-spec` | proofs for the admitted dep lemma bodies |
| `decompress_contract_only.json` | 2 | `contract-only` | `--no-anchor-proof` | decompress's orchestration proof + 3 invented helpers (contract frozen) |
| `decompress_bridge_specs.json` | 3 | `bridge-specs` | `--no-bridge-specs` | the 2 Montgomery↔Edwards map `spec fn` defs (pinned by frozen `to_edwards`) |
| `decompress_bridge_full.json` | 2 | `bridge-full` | `--no-bridge-lemmas` | all 10 decompress-path lemmas (every spec frozen — pure proof reconstruction) |
| `decompress_fullstack.json` | 2 | `bridge-full` | `--no-fullstack-proof` | the whole 3-layer decompress tree: 3 API proofs + 2 ristretto step proofs + 10 lemmas |

Verify any manifest reproduces its rung's cut without touching a tree:

```bash
./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json --surface
# or directly:
python3 peel.py --surface --manifest peel_manifests/decompress_bridge_full.json --depth 2
```

## Run one

```bash
DALEK_SRCREPO=/path/to/dalek-lite \
  ./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json \
      --run-id peel_bf_001 --detach
```

`peel_run.sh` builds a fresh peeled worktree from `--ref` (default `main`),
reads `experiment_mode` + `editable_files` out of peel's JSON, and launches
`run.py`. Recommended rounds/budget per rung (mirror the old defaults; pass
`--rounds`/`--budget` to override):

| rung | rounds | budget (min) |
|------|:---:|:---:|
| proof-only | 4 | 45 |
| contract-only | 6 | 90 |
| bridge-specs | 7 | 120 |
| bridge-full | 10 | 180 |
| fullstack | 16 | 240 |

## What's not here (and why)

- **`no-spec` / `no-lemmas` (gate-OFF `spec-proof`).** These have the agent
  *reconstruct contracts*, so they are not contract-pinned — soundness is judged
  by diffing against ground truth, not by a frozen guarantee. In peel terms they
  are a contract strip / whole-dir lemma delete with `pin: oracle:main` and
  `experiment_mode: spec-proof`. They remain driven by `launch_specgen.sh` /
  `demo_decompress.sh`; peel's pin rule is built for the *frozen-contract* rungs.
- **`--strip-to-fields` (the field-floor cut).** This is a whole-directory cut,
  not a hand-named lemma list, so it is *generated* (`peel.py --classify`) rather
  than hand-written — see **"The hardest setup"** at the top of this file for the
  one-liner and why it is the hardest setup despite being only depth 2.

See [`docs/spec_gen_runbook.md`](../docs/spec_gen_runbook.md) for the full
walkthrough.
