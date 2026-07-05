# Docker: per-agent isolation, shared CPU (GCP VM)

v1 of the containerized harness. **Goal:** isolate each agent's worktree work while
all agents share one CPU pool. Design rationale recorded in internal review thread T112.

> **Status:** Merged to `main` (`be10128`); reviewed in internal review thread T112.
> VM-smoke-tested on the benchmark VM — the image builds and the seal +
> launcher plumbing + a real single-container `run.py` proof round all run. (The
> sample proof task itself ended `PROCESS_CROSSTALK` — the agent backgrounded a
> verifier, a harness gate, not a Docker issue.) A full *parallel* multi-container
> sweep hasn't been run yet.

## The two requirements, and how they map

| Requirement | Mechanism |
|---|---|
| **Isolate per-agent worktree** | one container per agent; `/work` is a self-contained, history-sealed git repo with an **isolated object store**; own `/results` dir |
| **Share CPU across all agents** | **no** `--cpus` / `--cpuset-cpus` (those partition). Equal `--cpu-shares` → CFS arbitrates one shared pool; idle agents' cores flow to busy ones |

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
  (that would trip `TOOLING_DRIFT`); rebuild the image.
- **`install-verus.sh`** — Verus provisioning hook (point at the **same** build the
  VM uses so bake-time fingerprints == run-time; keeps the warm caches valid).
- **`seal_into_volume.sh`** — peeled worktree → isolated-store sealed `/work` volume.
- **`preflight.sh`** — one-shot offline-cache + seal validation before spawning N
  agents (a ro registry-cache miss fails hard; prove it once).
- **`run_agents.sh`** — host launcher: serialized `peel.py --worktree` → seal →
  `docker run -d --init` per manifest, concurrency-capped at `nproc`, rc-42
  (RATE_LIMITED) sweep-break, `--skip-existing` resume off a host-side ledger.

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

- inherits the **baked-warm** `CARGO_HOME` (registry cache + git-db) and
  `CARGO_TARGET_DIR` (compiled vstd + deps) via the image's **COW upper layer** — so
  both are **per-container** (never shared-writable; satisfies the no-shared-target
  rule, docs/diagnostics.md:414-422) yet start **warm**, and `target/` stays out of
  the `/work` volume;
- runs with `CARGO_NET_OFFLINE=true` → no network, no ro-index mutation.

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

## Tap and seed options

- **`--tap`** routes each container's `claude` through a per-agent host-side
  `claude-tap` proxy for inspectable traces and a live dashboard. It is
  best-effort: if the proxy cannot start or the Docker bridge is unavailable,
  the agent runs without tap. Use **`--require-tap`** when missing traces should
  fail the agent instead.
- **`--seed-wip <patch>`** applies a guarded WIP diff after peel and before seal,
  limited to the manifest's editable files. Use a distinct `--run-id` and
  `--work-base` for resumed seeded sweeps so their host ledger cannot collide
  with a clean run.
- **`--failure-memory-seed <failure_memory.json>`** copies prior retry guidance
  into each agent's private `/results` before prompt rendering.
- **`--operator-seed <patch>`** applies operator-owned scaffolding as part of the
  sealed baseline. It may touch frozen files, so treat those runs as diagnostic
  or non-scoreable unless the seed provenance is explicitly part of the claim.

## Quickstart

```bash
# 1. build the image. Provision Verus from a build-context pointing at the VM's
#    unpacked verus-x86-linux dir (preferred; the VERUS_TARBALL_URL hook is a
#    fallback for URL-based installs). Pin RUST_TOOLCHAIN to the VM's rustc. Supply
#    a warm curve25519-dalek checkout for a warm+offline image (omit for cold).
#    Requires BuildKit (default on modern Docker).
#    Smoke-tested 2026-06-27 on the benchmark VM (rustc 1.92.0, verus
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
export CLAUDE_CODE_OAUTH_TOKEN=...      # or use an authenticated keychain login
docker/run_agents.sh --image dalek-harness:v1 \
    --gitroot /path/to/dalek-lite --ref eval/admitted-start \
    --run-id sweep_001 --manifests-file /tmp/manifests.txt
```

`manifests.txt`: one peel manifest per line, `<manifest.json> [| pin | depth | minutes]`.
