# Docker: per-agent isolation, shared CPU (GCP VM)

v1 of the containerized harness. **Goal:** isolate each agent's worktree work while
all agents share one CPU pool. Design + debate: `AGENT_DEBATE.md` thread **T112**.

> **Status:** The Trust Core infrastructure profile is implemented and
> hostile-tested. The final local immutable image
> `cryptoprover-harness:trust-core-final` was rebuilt, and its then-current
> in-image harness digest matched the host harness digest through the full
> scored pre-model receipt chain. Every harness update requires a fresh image
> rebuild and a new equality receipt before use. Proxy death and interrupted cleanup fail
> closed, and the Claude CLI completed a non-proof POST-only proxy smoke.
> The real launch remains deliberately blocked until the user supplies an
> immutable budget registration. No Trust Core proof run has been launched.

## The two requirements, and how they map

| Requirement | Mechanism |
|---|---|
| **Isolate per-agent worktree** | one container per agent; `/work` is a self-contained, history-sealed git repo with an **isolated object store**; own `/results` dir |
| **Share CPU across all agents** | **no** `--cpus` / `--cpuset-cpus` (those partition). Equal `--cpu-shares` → CFS arbitrates one shared pool; idle agents' cores flow to busy ones |
| **Deny source retrieval over public Internet** | agent attaches only to an internal Docker network; a dual-homed, fixed-upstream provider proxy is the sole egress; pre-model probes verify hostname and direct-IP blocking |

## Why an isolated-store sealed repo (not a bind-mounted worktree, not strip-`.git`)

run.py's frozen-file audit (`_frozen_paths_changed_from_git`, run.py:1014-1042) is
**fail-closed on git** and runs every round in the whole-crate/bridge gates — so a
`.git`-less tree fails `FROZEN_EDIT` even on a clean run. A `git worktree`, on the
other hand, points its `.git` into the **shared** object store that still holds the
proven/ground-truth objects (reachable via reflog/sha → an oracle). The resolution
(T112): copy the source **working-tree bytes** (excluding `.git`/`target`) into a
fresh `git init` so the store contains **only** the sealed commit. `HEAD` == peeled
baseline (audit + revert work), and there is no reflog / no dangling sha / no `main`
ref back to proven source. Copying the working tree (not `git archive HEAD`) is
codex's 16:29 correction: the `admit.py --worktree` path writes the admitted skeleton
to the working tree *without committing*, so archiving `HEAD` there would emit proven
source — the working-tree copy covers both peel.py (committed) and admit.py
(uncommitted) builders and honors `--delete-fn` deletions. This is strictly
cleaner than peel's host seal, whose documented residual (peel.py:259-265) is exactly
the shared store + detach reflog we omit. `seal_into_volume.sh` implements + asserts
this (matches `_is_sealed_worktree`, run.py:1181-1198, and the `git fsck
--no-reflogs --unreachable` audit, run.py:1219-1238).

## Files

- **`Dockerfile`** — immutable image: pinned rust+verus+z3, python, claude CLI,
  read-only harness at `/opt/harness`, and **baked-warm** `CARGO_HOME` +
  `CARGO_TARGET_DIR`. Never overwrite the harness on a live container
  (memory:no-midrun-harness-hot-deploy → TOOLING_DRIFT); rebuild the image.
- **`install-verus.sh`** — Verus provisioning hook (point at the **same** build the
  VM uses so bake-time fingerprints == run-time; keeps the warm caches valid).
- **`seal_into_volume.sh`** — peeled worktree → isolated-store sealed `/work` volume.
- **`preflight.sh`** — one-shot offline-cache + seal validation before spawning N
  agents (a ro registry-cache miss fails hard; prove it once).
- **`provider_proxy.py` + `provider_proxy_policy.json`** — fixed Anthropic
  reverse proxy and hash-bound method/path/upstream policy; not a general
  forward proxy.
- **`sterility_probe.py`** — fail-closed pre-model inspection of network
  reachability, provider policy, isolated git store, mounts, and source-oracle
  paths.
- **`run_agents.sh`** — host launcher: serialized `peel.py --worktree` → seal →
  `docker run -d --init` per manifest, concurrency-capped at `nproc`, rc-42
  (RATE_LIMITED) sweep-break, `--skip-existing` resume off a host-side ledger,
  plus an optional provider-only internal network and sterility receipt.
- **`Dockerfile.trust-core-final`** — a thin immutable rebuild over the pinned
  base image that replaces both `/opt/harness` and the complete `/opt/verus`
  bundle. A scoreable build must provide explicit `patched_verus` and `warm`
  named contexts; either missing or incomplete context fails the build:

  ```bash
  docker build \
    --build-context patched_verus=/path/to/patched-verus-distribution \
    --build-context warm=/path/to/canonical-source \
    -f docker/Dockerfile.trust-core-final \
    -t cryptoprover-harness:trust-core-final-patched .
  ```

  Replacing the complete Verus distribution keeps `verus`, `rust_verify`,
  `cargo-verus`, vstd, and z3 coherent. Never overlay only the front-end
  executable onto an older bundle.
- **`../trusted_core_profile.py`** — composes canonical start, sterility,
  in-image harness, registration, lineage, terminal, usage, and campaign-state
  receipts. It is the sole authority for a scored launcher success/bank label.

## CPU sharing, precisely

- **Sharing** = CFS + equal `--cpu-shares 1024`. Correct under any oversubscription.
- **Thrash bound** (v1, patch-free): concurrency cap ≈ `nproc` + `CARGO_BUILD_JOBS`
  for the compile fan-out. Verus's own `--num-threads` is the dominant verify-phase
  vector but has **no env knob** and verus_check.py only plumbs `--rlimit` — so
  fine-grained verify-thread capping is the **deferred v2** upgrade (a ~2-line baked
  patch), gated on observed thrash, per the repo's "don't build on speculation"
  discipline.

## Cargo / offline (v1, patch-free — `--locked` intentionally dropped)

Correctness comes from **offline + a complete baked cache**, not `--locked`
(command-level `--locked` would need editing the integrity-sensitive verus_check.py
skill). Each container:

- inherits the **baked-warm** `CARGO_HOME` (registry cache + git-db) through the
  image's **COW upper layer**, while `CARGO_TARGET_DIR` starts empty and private
  to each container. The final-image build never compiles the ground-truth source
  into a reusable target cache: doing so could expose reference-derived metadata
  to the agent. This preserves the no-shared-target rule
  (docs/diagnostics.md:414-422), and `target/` stays out of the `/work` volume;
- runs with `CARGO_NET_OFFLINE=true`, which prevents Cargo registry access and
  ro-index mutation but is **not** container network isolation. Use
  `--provider-only-network` for the scoreable no-general-egress boundary.

The ro shared-registry bind-mount is a **pure disk-dedup optimization**, attempted
only after `preflight.sh` proves the offline cache resolves with the exact mounted
env. On preflight failure, fall back to the per-container baked `CARGO_HOME`.

## Ownership & read-only harness

Each container runs as the **invoking host UID:GID** (`docker run --user`), so `/work`
and `/results` are written with host ownership — no `chown`/`sudo` (codex T112 16:40
#3). The baked writable dirs (`/opt/cargo-home`, `/opt/cargo-target`,
`/opt/agent-home` = `HOME`) are world-writable so an arbitrary UID can do its COW
writes; `/opt/harness` is baked **read-only (0555)** so the agent cannot rewrite a
verification skill (a real TOOLING-gate hardening, not just wording).

## Lifecycle

`docker run --init` makes the container a single process group; `docker kill`
(or the host deadline) tears down claude + cargo verus + rust_verify + z3 + Monitor
loops in one shot — structurally replacing run.py's `killpg` dance.

## Resume / rate-limit

Per-agent `/results` dirs never share a registry → no read-modify-write race. The
launcher merges each agent's `result.json` into a host-side `_sweep_ledger.json`;
`--skip-existing` skips targets already `success` there. A 429 surfaces as run.py
exit 42 → the launcher halts the sweep (re-run with `--skip-existing` once the
window reopens).

## Quickstart

```bash
# 1. build the image. Provision Verus from a build-context pointing at the VM's
#    unpacked verus-x86-linux dir (preferred; the VERUS_TARBALL_URL hook is a
#    fallback for URL-based installs). Pin RUST_TOOLCHAIN to the VM's rustc. Supply
#    a warm curve25519-dalek checkout for a warm+offline image (omit for cold).
#    Requires BuildKit (default on modern Docker).
#    Smoke-tested 2026-06-27 on andtruth-benchmarks-x86 (rustc 1.92.0, verus
#    0.2026.01.14) — image builds; verus/cargo-verus/z3/claude/run.py all resolve.
docker build -t dalek-harness:v1 \
    --build-arg RUST_TOOLCHAIN=1.92.0 \
    --build-context verus=/home/<user>/verus-rel/verus-x86-linux \
    --build-context warm=/path/to/dalek-lite \
    -f docker/Dockerfile .
    # warm = workspace root (dalek-lite) OR the package dir (curve25519-dalek);
    # the build finds the package by its Cargo.toml name either way. Omit for a cold image.

# 2. preflight once (validates offline cache + a sample sealed work vol)
docker run --rm --init -e CARGO_NET_OFFLINE=true -e CARGO_HOME=/opt/cargo-home \
    -e CARGO_TARGET_DIR=/opt/cargo-target -v <sample-work>:/work \
    dalek-harness:v1 bash /opt/harness/docker/preflight.sh /work/curve25519-dalek

# 3. fan out the sweep (CPU shared, worktrees isolated)
export CLAUDE_CODE_OAUTH_TOKEN=...      # memory:run-claude-auth
docker/run_agents.sh --image dalek-harness:v1 \
    --gitroot /path/to/dalek-lite --ref eval/admitted-start \
    --run-id sweep_001 --manifests-file /tmp/manifests.txt
```

`manifests.txt`: one peel manifest per line, `<manifest.json> [| pin | depth | minutes]`.

## Provider-only and scored Trust Core preflight

The provider-only mode cannot be combined with `--tap` or `--registry-ro`:
either would add a host/network surface outside the fixed sidecar design. The
launcher requires a fresh run directory, resolves the policy hash and image
digest, creates an internal network, starts the fixed proxy, seals the peeled
tree into its isolated store, and runs the sterility probe using the exact
image/mount/network tuple.

Run the complete boundary without spending a model call:

```bash
docker/run_agents.sh \
    --image cryptoprover-harness:trust-core-final \
    --gitroot /path/to/dalek-lite \
    --ref 103b92b9ddd93a6a904f7c86a48ec911cf533717 \
    --run-id trust_core_preflight_001 \
    --manifests-file /tmp/trust-core-manifests.txt \
    --provider-only-network \
    --provider-policy docker/provider_proxy_policy.json \
    --sterility-preflight-only
```

Successful preflight produces `sterility_receipt.json` and
`sterility_envelope.json` under each agent directory and then exits before
`run.py` or the model starts; it therefore does not require provider
credentials. Any provider-only peel, seal, probe, or launch-boundary failure
halts the sweep with exit 44 instead of being skipped. A scored campaign must
use the final rebuilt image digest and still pass the accounting and terminal
acceptance gates; this preflight alone is necessary but not sufficient for
scoreability.

The full scored profile adds a fixed campaign identity and a run-specific,
immutable launch registration:

```bash
docker/run_agents.sh \
    --image cryptoprover-harness:trust-core-final \
    --gitroot /path/to/canonical/source-store \
    --ref 103b92b9ddd93a6a904f7c86a48ec911cf533717 \
    --run-id trust_core_leg_001 \
    --manifests-file /path/to/trust-core-manifests.txt \
    --provider-only-network \
    --trusted-core-profile \
    --model <registered-model> \
    --agent-max-turns 50 \
    --campaign-spec peel_manifests/trusted_core_campaign_v1.json \
    --launch-registration /path/to/user-authorized-registration.json
```

For a banked continuation, also pass the predecessor patch, reusable promotion
receipt, and immediately preceding campaign-state receipt:

```text
--seed-wip <patch> --seed-receipt <promotion_receipt.json>
--predecessor-terminal <terminal_validation.json>
--campaign-state <campaign_state.json>
--root-start-envelope <original_start_envelope.json>
```

The original root envelope is stable lineage authority. Every continuation
still rebuilds the canonical peel from the registered source and writes
`replayed_start_envelope.json` plus `root_replay_validation.json`; only after
their deterministic campaign/manifest/pre/post receipts match is the
predecessor patch applied. This avoids treating a timestamp-dependent new seal
commit as a new scientific lineage.

Before the model starts, the profile writes `start_envelope.json`,
`sterility_envelope.json`, and `/results/_lineage_context.json`. It verifies
that the exact pinned image contains the same executable harness bytes as the
registered host harness. That receipt covers required entry points, every
top-level Python/shell launcher and prompt, and recursive Python/shell/JSON
inputs under `lib/`, `skills/`, `docker/`, and `scripts/` (excluding generated
`__pycache__` files and rejecting symlinked inputs), so a nested helper cannot
escape the image/host binding. Both `docker/Dockerfile` and the scored
`docker/Dockerfile.trust-core-final` mirror that authority with the explicit
Git-tracked top-level `*.py`/`*.sh`/`prompt*.md` set plus the `scripts/` tree.
The test suite derives that set from `git ls-files` and requires both images to
match it. A dirty, untracked top-level harness input remains visible to the host
receipt but is not silently baked; host/image equality fails closed instead.
At reap it seals `usage_audit.json`, validates only an
`ACCEPTED` or `BANKED_PARTIAL` promotion over the current tree, and writes
`terminal_validation.json` plus `campaign_state.json`. A plain
`end_reason: COMPLETE` never produces launcher success. Terminal/accounting
failure exits 45; provider boundary failure exits 44.

The checked-in campaign deliberately leaves `K`, cost, and wall time null.
`stage3_local_evidence/trust_core_synthetic_registration.json` is explicitly a
zero-proof preflight fixture, not user budget authorization and not a valid
reason to launch a provider-backed leg.

The registration also fixes the execution envelope (model/backend, rounds,
positive `agent_max_turns`, per-leg minutes, Verus rlimit, experiment mode,
memory, CPU shares, Cargo jobs, and one-leg concurrency). The launcher compares
those values before spending, forwards the same cap to `run.py`, and forces one
canonical leg at a time. A capped Claude result retains its exact terminal cost
and the next round starts from a fresh post-gate handoff. Only H0 is currently scoreable;
unregistered operator brief/provenance/pre-edit channels fail closed.

## Durable campaign supervision

`docker/trusted_core_supervisor.py` is the outer, restart-safe liveness loop for
a registered Trust Core package. Its package JSON fixes the launch argv,
prelaunch active-state/sterility check, result and audit paths, run-ID prefix,
and SHA256s of every immutable input. The `package_id` is a domain-separated
SHA256 that content-addresses the entire package (excluding only that ID field),
and the supervisor binds the persisted ID across retries before another launch.
Packages minted before this domain-separated, full-content identity rule are
intentionally incompatible; regenerate them from their sealed inputs rather
than relabeling an archival package. The state, ledger, and lock directory are
trusted operator-controlled storage; package-ID continuity does not defend
against an actor who can rewrite supervisor state. Each attempt receives a
fresh run ID and
every launch, decision, wait, error, and next action is fsync-appended to a JSONL
ledger; mutable state is atomically replaced under a nonblocking process lock.

Before spending, the supervisor reads finite positive campaign ceilings from
the package's immutable launch registration. Every result-bearing attempt must
carry sealed wall-time segments and either complete accounting with zero
unresolved streams or an explicitly accepted reconciliation/conservative
amendment that does not undercut recorded receipts. (`not_run` is confined to
the exact zero-provider premodel gate.) Accepted amendments may still name
unresolved streams because the operator-approved debit is their replacement
authority. Cost and active wall time accumulate in durable state across
retries and successor packages; an over-ceiling terminal is stopped, and a
retry/advance at the ceiling is stopped before another launch.

An accounted `RATE_LIMITED`, `TRANSPORT_ERROR`, or `RETRY_EXHAUSTED` result
retries the same package. A provider `resetsAt` found in the raw stream wins
over exponential fallback and is re-read after every rejection. Restarting the
supervisor preserves a future wake time instead of probing immediately. A
reusable `BANKED_PARTIAL` invokes the package's sealed `next_package_argv` and
will not continue unless that generator emits a hash-valid successor package.
`ACCEPTED` completes; budget/plateau stops, integrity failures, unresolved
accounting, nonreusable terminals, and unknown states stop fail-closed.

Run it as a systemd service with `Restart=on-failure`,
`SuccessExitStatus=3`, and `RestartPreventExitStatus=2 3`: unexpected process
death restarts the loop, while an explicit evidence failure or campaign stop
stays stopped for review.
The supervisor capability alone does not authorize provider use. Its bytes,
successor generator, package inputs, image digest, and registration supervisor
amendment remain part of the scored preflight/reseal boundary.
