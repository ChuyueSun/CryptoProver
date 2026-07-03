# syntax=docker/dockerfile:1
#
# Immutable image for the per-agent Verus proof-synthesis harness (GCP VM).
#
# Design (see docker/README.md for the rationale):
#   - The harness (run.py / skills / lib / peel.py / admit.py / prompt.md) is baked
#     in READ-ONLY at /opt/harness. NEVER overwrite it on a live container — editing
#     a verification skill mid-run trips the TOOLING gate (-> TOOLING_DRIFT) and
#     violates memory:no-midrun-harness-hot-deploy. Rebuild the image instead.
#   - Toolchain (rust + verus + z3) versions are PINNED so bake-time fingerprints
#     equal run-time fingerprints — that is what makes the warm CARGO_HOME / warm
#     CARGO_TARGET_DIR valid across the bake->run boundary (codex's fingerprint
#     concern, T112).
#   - A complete warm CARGO_HOME and a warm CARGO_TARGET_DIR are baked in. At run
#     time each container inherits them via the image's COW upper layer, so they are
#     per-container (never shared-writable) AND start warm. CARGO_NET_OFFLINE=true
#     then guarantees no network / no ro-index mutation: correctness comes from the
#     complete baked cache, NOT from --locked (dropped in T112).
#
# Build (warm + offline image — recommended; smoke-tested 2026-06-27 on the VM):
#   docker build -t dalek-harness:v1 \
#     --build-arg RUST_TOOLCHAIN=1.92.0 \
#     --build-context verus=/path/to/verus-x86-linux \
#     --build-context warm=/path/to/dalek-lite \
#     -f docker/Dockerfile .
# Pin RUST_TOOLCHAIN to the VM's rustc (1.92.0 on the benchmark VM). Provision
# Verus via the `verus` build-context (preferred); --build-arg VERUS_TARBALL_URL=<url>
# is the fallback. The `warm` context may be EITHER the workspace root (dalek-lite, with
# a curve25519-dalek/ member) OR the package dir (curve25519-dalek) — the build locates
# the package by its Cargo.toml name. Omit --build-context warm=... for a COLD image (still builds;
# cold/online on first per-container build). Requires BuildKit (default modern Docker).
#
# The Verus install is intentionally a hook (install-verus.sh) rather than a
# hard-coded URL: provision it with the SAME known-good toolchain the VM already
# uses (memory:run-env-setup), so we don't guess a release URL that may 404.

ARG RUST_TOOLCHAIN=1.79.0

# Fallback empty `warm` stage: `docker build --build-context warm=<path>` overrides
# this; without it, COPY --from=warm yields an empty dir and warming is skipped.
FROM scratch AS warm

# Fallback empty `verus` stage: `docker build --build-context verus=<unpacked
# verus-x86-linux dir>` overrides this to bring the toolchain in by COPY (what the
# GCP VM has). Without it, the install-verus.sh URL hook is used instead.
FROM scratch AS verus

FROM rust:${RUST_TOOLCHAIN}-bookworm AS base

# --- system deps -----------------------------------------------------------
# z3 + python3.11 + git/tar (seal recipe) + nodejs/npm (claude CLI).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        z3 \
        git ca-certificates curl tar xz-utils \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# --- claude CLI ------------------------------------------------------------
# OAuth token is injected at RUN time (-e CLAUDE_CODE_OAUTH_TOKEN), never baked
# (memory:run-claude-auth).
RUN npm install -g @anthropic-ai/claude-code

# --- verus -----------------------------------------------------------------
# Provisions `verus` + the `cargo verus` subcommand shim. Mirror the VM toolchain.
ARG VERUS_TARBALL_URL=""
# Preferred: COPY the toolchain from a `verus` build-context (empty scratch by
# default). Fallback: the install-verus.sh URL hook. The whole verus-x86-linux dir
# (verus + cargo-verus + z3 + vstd + verus-root) is copied intact so cargo-verus
# resolves its siblings; PATH then exposes `verus`, `cargo verus`, `z3`.
COPY --from=verus . /opt/verus/
COPY docker/install-verus.sh /tmp/install-verus.sh
RUN if [ -x /opt/verus/verus ]; then \
        echo "verus: provisioned from build-context"; \
    elif [ -n "${VERUS_TARBALL_URL}" ]; then \
        VERUS_TARBALL_URL="${VERUS_TARBALL_URL}" bash /tmp/install-verus.sh; \
    else \
        echo "ERROR: no verus build-context and no VERUS_TARBALL_URL" >&2; exit 1; \
    fi
ENV PATH="/opt/verus:${PATH}"

# --- harness (read-only) ---------------------------------------------------
# Copy only the code the agent runs; results/ and worktrees live on mounted vols.
WORKDIR /opt/harness
COPY run.py admit.py peel.py replay.py prompt.md prompt_decompose.md ./
COPY skills/ ./skills/
COPY lib/ ./lib/
COPY peel_manifests/ ./peel_manifests/
# preflight.sh runs INSIDE the container (README path /opt/harness/docker/preflight.sh);
# the rest of docker/ is harmless to carry. seal_into_volume.sh runs on the HOST.
COPY docker/ ./docker/
# Discoverable-skills symlink (the CLAUDE.md one-time setup), local to the image.
RUN mkdir -p /opt/harness/.claude/skills \
    && ln -sfn /opt/harness/skills /opt/harness/.claude/skills/dalek-lite-mvp

# --- cargo home + warm caches (baked, inherited per-container via COW) ------
ENV CARGO_HOME=/opt/cargo-home
ENV CARGO_TARGET_DIR=/opt/cargo-target
ENV CARGO_NET_OFFLINE=true

# Warm the registry cache, git-db, and target/ (incl. vstd) ONCE at bake time
# against a representative curve25519-dalek checkout, so run-time builds are warm and
# offline. An explicit `cargo fetch` (NOT `|| true`) runs first so the baked registry
# is COMPLETE for the whole workspace lock — this is exactly what preflight.sh:38-42
# (`cargo fetch --offline`) gates on; a member-only `cargo verus verify -p` warm can
# leave sibling-member deps unfetched (codex 21:23). The warm verify keeps `|| true`
# (proof/syntax errors must not block cache baking).
# The warm source is brought in as a BuildKit named build context (NOT a
# repo-tracked fixture — codex T112 16:40 blocker #4), so the build is reproducible
# from an explicit input without committing a multi-MB checkout into the repo:
#
#   docker build --build-context warm=/path/to/dalek-lite -t dalek-harness:v1 \
#                -f docker/Dockerfile .   # warm = workspace root OR package dir
#
# If the warm context is omitted, the build still succeeds (empty stage) — the image
# is then COLD on first per-container build and preflight.sh will report the offline
# cache miss; supply the context to get a warm/offline image.
COPY --from=warm . /opt/warm-src/
# Locate the curve25519-dalek package by its Cargo.toml `name` (NOT by directory
# path) so the warm context may be EITHER the workspace root (curve25519-dalek/ is a
# subdir) OR the package dir itself (Cargo.toml at /opt/warm-src) — codex P2. The old
# `-path '*curve25519-dalek*'` match missed the package-dir case and `cd` fell through
# to /opt/harness, failing the (non-`|| true`) cargo fetch.
RUN --mount=type=cache,target=/root/.cache \
    set -eux; \
    pkg=$(grep -rlE 'name *= *"curve25519-dalek"' /opt/warm-src --include=Cargo.toml 2>/dev/null | head -1 || true); \
    if [ -n "$pkg" ]; then \
        cd "$(dirname "$pkg")"; \
        CARGO_NET_OFFLINE=false cargo fetch; \
        CARGO_NET_OFFLINE=false cargo verus verify -p curve25519-dalek || true; \
    else \
        echo "WARN: no warm build-context (or no curve25519-dalek package) — image cold/online on first build"; \
    fi; \
    rm -rf /opt/warm-src

# No baked USER: the launcher runs each container as the INVOKING host UID:GID
# (`docker run --user`), so /work + /results get host ownership with no chown/sudo
# (codex T112 16:40 blocker #3). For that arbitrary UID to work we make the writable
# baked dirs world-writable, give it a writable HOME, and keep the harness genuinely
# READ-ONLY (0555 — hardening ask #2; the agent cannot rewrite a verification skill).
# mkdir the cargo dirs explicitly: with no warm build-context nothing has created
# them yet (ENV only names them), so the chmod would fail (caught by VM build smoke).
RUN mkdir -p /opt/cargo-home /opt/cargo-target /opt/agent-home \
    && chmod -R 0777 /opt/cargo-home /opt/cargo-target /opt/agent-home \
    && chmod -R a-w,a+rX /opt/harness

# PID-1 reaping is provided by `docker run --init` (Docker's bundled tini) — the
# launcher (run_agents.sh) always passes --init. The container is then a single
# process group: `docker kill` tears down claude + cargo verus + rust_verify + z3 +
# Monitor loops in one shot, structurally replacing run.py's killpg dance (T112 (4)).
# No ENTRYPOINT: the launcher supplies the full `python /opt/harness/run.py ...` cmd.
WORKDIR /work
