#!/usr/bin/env bash
#
# Host launcher: fan out one CONTAINER per peel manifest, each with an isolated
# sealed /work tree and its own /results, all sharing one CFS CPU pool.
#
# This is the Docker counterpart of peel_run.sh (build-side) + launch.sh (sweep).
# Per the T112 consensus it changes NOTHING in run.py / skills / lib — the harness
# runs unmodified inside the image.
#
#   per target:  peel.py --worktree   (SERIALIZED under a repo lock — worktree
#                                       creation mutates shared repo metadata)
#                -> seal_into_volume   (archive peeled tree into an ISOLATED-store
#                                       sealed repo = the /work volume)
#                -> docker run -d --init  (CFS-shared CPU, per-container target/)
#
# CPU sharing (T112): NO --cpus / --cpuset-cpus (those partition). Equal
# --cpu-shares + a concurrency cap near core count + CARGO_BUILD_JOBS for the
# compile fan-out. Verus --num-threads is the deferred v2 knob.
#
# Resume: --skip-existing reads a HOST-side sweep ledger (per-agent /results dirs
# never share a registry, so there is no in-container read-modify-write race; the
# launcher merges each agent's result.json into the ledger as containers exit).
#
# Usage:
#   docker/run_agents.sh \
#       --image dalek-harness:v1 --gitroot /path/to/dalek-lite \
#       --ref eval/admitted-start --run-id sweep_001 \
#       --manifests-file /tmp/manifests.txt
#
#   manifests.txt: one per line  ->  <manifest.json> [| pin | depth | minutes]
#
# RESUME (--seed-wip <patch> --seed-receipt <promotion_receipt.json>): apply a WIP unified git diff into each peeled
# host worktree AFTER peel, BEFORE seal, so the container resumes from partially-
# reconstructed proofs. A normal resume additionally requires an immutable
# ACCEPTED promotion receipt and the resulting full source-tree hash must match
# it exactly. The patch is GUARDED to touch only the manifest's editable_files
# and must apply cleanly. `--seed-override-reason` makes a deliberate new seed
# explicit rather than silently reusing an unbound residue. Use a SEPARATE
# --run-id/--work-base from the clean sweep (the host ledger keys status by
# target_id, so a same-target clean+resume in one sweep would clobber each
# other). Typical:
#   git -C <peeled-repo> diff <baseline> <wip-ref> > /tmp/wip.patch
#   docker/run_agents.sh ... --run-id resume_001 --seed-wip /tmp/wip.patch \
#       --seed-receipt /path/to/promotion_receipt.json
#
# To carry prior retry guidance into a resumed isolated agent, pass:
#   --failure-memory-seed /path/to/previous/results/failure_memory.json
# The seed is copied into each new agent's private /results before run.py renders
# prompt_rendered.md. Do not mount one shared writable failure_memory.json across
# concurrent agents.
#
# OPERATOR SEED (--operator-seed <patch>): apply operator-owned source fixes or
# off-lane compile-debt stubs AFTER peel, BEFORE seal, as the sealed baseline.
# Unlike --seed-wip, this MAY touch frozen files; use it only for non-scoreable
# launch scaffolding whose provenance is recorded in agent_*/operator_seed.*.
#
# TAP (--tap): route each container's claude through a per-agent host-side
# claude-tap proxy so the run is inspectable (dashboard + trace DB), the Docker
# analogue of peel_run.sh's tap. Best-effort: any tap problem (no claude-tap,
# busy port, no bridge gateway) warns and runs that agent WITHOUT tap. The proxy
# binds the docker bridge gateway IP (override DALEK_TAP_HOST=0.0.0.0 to expose
# on all interfaces); the container reaches it via host.docker.internal. Ports
# are DALEK_TAP_BASE_PORT+idx / DALEK_TAP_LIVE_BASE+idx. Traces are operator-only
# (prompt/source-bearing) and stay host-side — never mounted into /work|/results.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/.." && pwd)

IMAGE=dalek-harness:v1
GITROOT=""; REF="eval/admitted-start"; RUN_ID=""
MANIFESTS_FILE=""
SEED_WIP=""                      # optional resume seed: guarded to editable files
SEED_RECEIPT=""                  # immutable ACCEPTED promotion receipt for --seed-wip
SEED_OVERRIDE_REASON=""          # disclosed reason for a deliberately non-identical/new seed
FAILURE_MEMORY_SEED=""           # optional prior failure_memory.json copied into private /results
OPERATOR_SEED=""                 # optional operator seed: source-only, may touch frozen files
TAP=0                            # --tap: route each container's claude through a per-agent claude-tap proxy
TAP_BASE_PORT="${DALEK_TAP_BASE_PORT:-58970}"  # host proxy port per agent = base+idx
TAP_LIVE_BASE="${DALEK_TAP_LIVE_BASE:-8810}"   # host dashboard port per agent = base+idx
TAP_OUT="${DALEK_TAP_OUT:-/tmp/tap-traces}"
TAP_LOG="${DALEK_TAP_LOG:-/tmp/claude-tap.docker.log}"
TAP_HOST="${DALEK_TAP_HOST:-}"   # proxy bind addr; default = docker bridge gateway IP (discovered)
BRIDGE_GW=""                     # docker bridge gateway IP (filled by discover_bridge_gw)
WORK_BASE="/srv/agents"          # host dir holding per-agent {work,results}
CPU_SHARES=1024
MEM="8g"
ROUNDS=10
AGENT_MAX_TURNS=50              # costed Claude boundary; fresh session follows
MAX_PARALLEL="$(nproc 2>/dev/null || echo 4)"
CARGO_JOBS=4                     # CARGO_BUILD_JOBS: compile fan-out per container
REGISTRY_RO=""                   # optional host registry cache; only after preflight
SKIP_EXISTING=0
REQUIRE_TAP=0                    # --require-tap: fail-closed tap — TAP_FAIL + abort if tap doesn't engage
VSTD_CONTAINER="${DALEK_VSTD_CONTAINER:-}"   # baked vstd path in image, optional
MODEL=""                         # optional Claude model forwarded to run.py
VERUS_RLIMIT="80"
PROVIDER_ONLY=0                  # internal agent network + fixed-upstream proxy sidecar
PROVIDER_POLICY="$repo/docker/provider_proxy_policy.json"
PROVIDER_POLICY_SHA256=""
PROVIDER_NETWORK=""
PROVIDER_PROXY_NAME=""
IMAGE_DIGEST=""
STERILITY_PREFLIGHT_ONLY=0
TRUSTED_CORE_PROFILE=0
CAMPAIGN_SPEC=""
LAUNCH_REGISTRATION=""
CAMPAIGN_STATE=""
ROOT_START_ENVELOPE=""
PREDECESSOR_TERMINAL=""
PROFILE_MAX_COST_USD=""
PROFILE_MAX_WALL_SECONDS=""
PROFILE_PLATEAU_K=""
PROFILE_MODEL=""
PROFILE_MINUTES=""
PROFILE_EXPERIMENT_MODE=""
LAUNCH_REGISTRATION_SHA256=""

die() { echo "run_agents: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --gitroot) GITROOT="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --manifests-file) MANIFESTS_FILE="$2"; shift 2 ;;
        --seed-wip) SEED_WIP="$2"; shift 2 ;;
        --seed-receipt) SEED_RECEIPT="$2"; shift 2 ;;
        --seed-override-reason) SEED_OVERRIDE_REASON="$2"; shift 2 ;;
        --failure-memory-seed) FAILURE_MEMORY_SEED="$2"; shift 2 ;;
        --operator-seed) OPERATOR_SEED="$2"; shift 2 ;;
        --tap) TAP=1; shift ;;
        --no-tap) TAP=0; shift ;;
        --require-tap) TAP=1; REQUIRE_TAP=1; shift ;;   # fail-closed: abort the agent if tap can't engage
        --work-base) WORK_BASE="$2"; shift 2 ;;
        --cpu-shares) CPU_SHARES="$2"; shift 2 ;;
        --mem) MEM="$2"; shift 2 ;;
        --rounds) ROUNDS="$2"; shift 2 ;;
        --agent-max-turns) AGENT_MAX_TURNS="$2"; shift 2 ;;
        --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
        --cargo-jobs) CARGO_JOBS="$2"; shift 2 ;;
        --registry-ro) REGISTRY_RO="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --verus-rlimit) VERUS_RLIMIT="$2"; shift 2 ;;
        --provider-only-network) PROVIDER_ONLY=1; shift ;;
        --provider-policy) PROVIDER_POLICY="$2"; shift 2 ;;
        --sterility-preflight-only) STERILITY_PREFLIGHT_ONLY=1; shift ;;
        --trusted-core-profile) TRUSTED_CORE_PROFILE=1; shift ;;
        --campaign-spec) CAMPAIGN_SPEC="$2"; shift 2 ;;
        --launch-registration) LAUNCH_REGISTRATION="$2"; shift 2 ;;
        --campaign-state) CAMPAIGN_STATE="$2"; shift 2 ;;
        --root-start-envelope) ROOT_START_ENVELOPE="$2"; shift 2 ;;
        --predecessor-terminal) PREDECESSOR_TERMINAL="$2"; shift 2 ;;
        --skip-existing) SKIP_EXISTING=1; shift ;;
        *) die "unknown arg: $1" ;;
    esac
done
[ -n "$GITROOT" ] || die "--gitroot required"
[ -n "$RUN_ID" ] || die "--run-id required"
command -v python3 >/dev/null || die "python3 not found on PATH"
RUN_ID_ERROR=$(PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m lib.results validate-run-id "$RUN_ID" 2>&1
) || { printf '%s\n' "$RUN_ID_ERROR" >&2; exit 2; }
[ "${#RUN_ID}" -le 40 ] || die "--run-id exceeds 40 characters (Docker network/name safety)"
[ -n "$MANIFESTS_FILE" ] || die "--manifests-file required"
[ -f "$MANIFESTS_FILE" ] || die "--manifests-file not found: $MANIFESTS_FILE"
case "$AGENT_MAX_TURNS" in
    *[!0-9]*|"") die "--agent-max-turns must be a positive integer" ;;
esac
[ "$AGENT_MAX_TURNS" -gt 0 ] \
    || die "--agent-max-turns must be a positive integer"
[ -z "$SEED_WIP" ] || [ -f "$SEED_WIP" ] || die "--seed-wip patch not found: $SEED_WIP"
[ -z "$SEED_RECEIPT" ] || [ -f "$SEED_RECEIPT" ] || die "--seed-receipt not found: $SEED_RECEIPT"
[ -z "$FAILURE_MEMORY_SEED" ] || [ -f "$FAILURE_MEMORY_SEED" ] || die "--failure-memory-seed not found: $FAILURE_MEMORY_SEED"
[ -z "$OPERATOR_SEED" ] || [ -f "$OPERATOR_SEED" ] || die "--operator-seed patch not found: $OPERATOR_SEED"
[ -z "$SEED_WIP" ] || [ -n "$SEED_RECEIPT" ] || [ -n "$SEED_OVERRIDE_REASON" ] \
    || die "--seed-wip requires --seed-receipt or an explicit --seed-override-reason"
[ -z "$SEED_RECEIPT" ] || [ -n "$SEED_WIP" ] \
    || die "--seed-receipt is meaningful only with --seed-wip"
[ -z "$SEED_WIP" ] || SEED_WIP="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$SEED_WIP")"
[ -z "$SEED_RECEIPT" ] || SEED_RECEIPT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$SEED_RECEIPT")"
[ -z "$OPERATOR_SEED" ] || OPERATOR_SEED="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$OPERATOR_SEED")"
[ "$STERILITY_PREFLIGHT_ONLY" = "1" ] \
    || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
    || die "CLAUDE_CODE_OAUTH_TOKEN not set (memory:run-claude-auth)"
command -v docker >/dev/null || die "docker not on PATH"
if [ "$PROVIDER_ONLY" = "1" ]; then
    [ -f "$PROVIDER_POLICY" ] || die "provider policy not found: $PROVIDER_POLICY"
    [ "$TAP" = "0" ] || die "--provider-only-network cannot use host --tap; the fixed sidecar is the sole egress"
    [ -z "$REGISTRY_RO" ] || die "--provider-only-network forbids extra --registry-ro mounts"
    PROVIDER_POLICY_SHA256="$(python3 - "$repo" "$PROVIDER_POLICY" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from docker.provider_proxy import load_policy
print(load_policy(Path(sys.argv[2]))[1])
PY
)" || die "invalid provider proxy policy"
    IMAGE_DIGEST="$(docker image inspect "$IMAGE" --format '{{.Id}}')" \
        || die "cannot resolve immutable image digest: $IMAGE"
fi
[ "$STERILITY_PREFLIGHT_ONLY" = "0" ] || [ "$PROVIDER_ONLY" = "1" ] \
    || die "--sterility-preflight-only requires --provider-only-network"
if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
    [ "$PROVIDER_ONLY" = "1" ] \
        || die "--trusted-core-profile requires --provider-only-network"
    [ -f "$CAMPAIGN_SPEC" ] || die "--campaign-spec required for trusted-core profile"
    [ -f "$LAUNCH_REGISTRATION" ] \
        || die "--launch-registration required for trusted-core profile"
    LAUNCH_REGISTRATION_SHA256="$(sha256sum "$LAUNCH_REGISTRATION" | awk '{print $1}')"
    [ -z "$CAMPAIGN_STATE" ] || [ -f "$CAMPAIGN_STATE" ] \
        || die "--campaign-state not found: $CAMPAIGN_STATE"
    [ -z "$ROOT_START_ENVELOPE" ] || [ -f "$ROOT_START_ENVELOPE" ] \
        || die "--root-start-envelope not found: $ROOT_START_ENVELOPE"
    [ -z "$PREDECESSOR_TERMINAL" ] || [ -f "$PREDECESSOR_TERMINAL" ] \
        || die "--predecessor-terminal not found: $PREDECESSOR_TERMINAL"
    [ -z "$FAILURE_MEMORY_SEED" ] \
        || die "trusted-core profile forbids failure-memory seeds"
    [ -z "$OPERATOR_SEED" ] || die "trusted-core profile forbids operator seeds"
    [ -z "$SEED_OVERRIDE_REASON" ] \
        || die "trusted-core profile forbids seed overrides"
    [ "$SKIP_EXISTING" = "0" ] \
        || die "trusted-core profile forbids mutable skip-existing reuse"
    [ -n "$MODEL" ] || die "trusted-core profile requires an explicit --model"
    [ "$(awk 'NF && $1 !~ /^#/ {count++} END {print count+0}' "$MANIFESTS_FILE")" = "1" ] \
        || die "trusted-core profile requires exactly one canonical manifest leg"
    [ -z "$SEED_WIP" ] || [ -n "$SEED_RECEIPT" ] \
        || die "trusted-core resume requires a predecessor receipt"
    if [ -n "$SEED_WIP" ]; then
        [ -n "$CAMPAIGN_STATE" ] \
            || die "trusted-core resume requires the preceding --campaign-state"
        [ -n "$ROOT_START_ENVELOPE" ] \
            || die "trusted-core resume requires the original --root-start-envelope"
        [ -n "$PREDECESSOR_TERMINAL" ] \
            || die "trusted-core resume requires --predecessor-terminal"
    else
        [ -z "$CAMPAIGN_STATE" ] \
            || die "fresh trusted-core root cannot import a campaign state"
        [ -z "$ROOT_START_ENVELOPE" ] \
            || die "fresh trusted-core root creates its own start envelope"
        [ -z "$PREDECESSOR_TERMINAL" ] \
            || die "fresh trusted-core root cannot import a predecessor terminal"
    fi
    read -r PROFILE_MAX_COST_USD PROFILE_MAX_WALL_SECONDS PROFILE_PLATEAU_K \
        PROFILE_MODEL PROFILE_MINUTES PROFILE_EXPERIMENT_MODE < <(
        python3 - "$LAUNCH_REGISTRATION" <<'PY'
import json
import sys
d = json.load(open(sys.argv[1]))
b = d.get("budget") or {}
cost, wall, plateau = (
    b.get("max_cost_usd"), b.get("max_wall_seconds"), b.get("plateau_k"),
)
if any(
    not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0
    for v in (cost, wall)
):
    raise SystemExit(1)
if not isinstance(plateau, int) or isinstance(plateau, bool) or plateau <= 0:
    raise SystemExit(1)
e = d.get("execution") or {}
required = (
    "model", "max_task_minutes", "experiment_mode", "rounds",
    "verus_rlimit", "cargo_jobs", "cpu_shares", "memory", "max_parallel",
    "agent_max_turns",
)
if any(e.get(key) is None for key in required):
    raise SystemExit(1)
if d.get("hint_level") != "H0" or e.get("agent_backend") != "claude":
    raise SystemExit(1)
print(cost, wall, plateau, e["model"], e["max_task_minutes"], e["experiment_mode"])
PY
    ) || die "trusted-core launch registration lacks positive budget values"
    python3 - "$LAUNCH_REGISTRATION" "$MODEL" "$ROUNDS" "$VERUS_RLIMIT" \
        "$CARGO_JOBS" "$CPU_SHARES" "$MEM" "$AGENT_MAX_TURNS" <<'PY' \
        || die "trusted-core launch execution does not match its registration"
import json
import sys
d = json.load(open(sys.argv[1]))
e = d["execution"]
actual = {
    "model": sys.argv[2],
    "rounds": int(sys.argv[3]),
    "verus_rlimit": float(sys.argv[4]),
    "cargo_jobs": int(sys.argv[5]),
    "cpu_shares": int(sys.argv[6]),
    "memory": sys.argv[7],
    "max_parallel": 1,
    "agent_max_turns": int(sys.argv[8]),
}
for key, value in actual.items():
    registered = e.get(key)
    if key == "verus_rlimit":
        if float(registered) != value:
            raise SystemExit(f"{key}: {registered!r} != {value!r}")
    elif registered != value:
            raise SystemExit(f"{key}: {registered!r} != {value!r}")
PY
    python3 - "$repo" "$LAUNCH_REGISTRATION" <<'PY' \
        || die "trusted-core launch credential does not match its registration"
import json
import os
import sys
sys.path.insert(0, sys.argv[1])
from lib.provenance import validate_credential_identity

with open(sys.argv[2]) as fh:
    registration = json.load(fh)
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if not token:
    raise SystemExit("CLAUDE_CODE_OAUTH_TOKEN is unset")
validate_credential_identity(registration, token)
PY
    MAX_PARALLEL=1
fi
SEED_RECEIPT_TREE_HASH=""
if [ -n "$SEED_RECEIPT" ]; then
    SEED_RECEIPT_TREE_HASH="$(python3 - "$repo" "$SEED_RECEIPT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from lib.provenance import reusable_seed_authority
print(reusable_seed_authority(Path(sys.argv[2]))["tree_hash"])
PY
)" || die "--seed-receipt must be an immutable reusable ACCEPTED/BANKED_PARTIAL promotion receipt"
fi
if [ -n "$FAILURE_MEMORY_SEED" ]; then
    python3 - "$FAILURE_MEMORY_SEED" <<'PY' || die "--failure-memory-seed is not valid JSON with a records array: $FAILURE_MEMORY_SEED"
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
if not isinstance(data, dict) or not isinstance(data.get("records"), list):
    raise SystemExit(1)
PY
fi

sweep_dir="$WORK_BASE/$RUN_ID"
ledger="$sweep_dir/_sweep_ledger.json"
repo_lock="$sweep_dir/.peel_repo.lock"     # serialize peel worktree creation
[ "$PROVIDER_ONLY" = "0" ] || [ ! -e "$sweep_dir" ] \
    || die "provider-only run requires a fresh run directory: $sweep_dir"
mkdir -p "$sweep_dir"
[ -f "$ledger" ] || echo '{}' > "$ledger"

ledger_status() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],{}).get("status",""))' "$ledger" "$1"; }
ledger_set() {    python3 - "$ledger" "$1" "$2" <<'PY'
import json,sys
p,k,v=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(p)); d[k]={"status":v}; json.dump(d,open(p,"w"),indent=2)
PY
}

# GNU flock is present on the Linux campaign host but not on stock macOS.
# Keep one serialization contract on both: the Python fallback holds the same
# advisory file lock for the complete child lifetime.
repo_locked() {
    if command -v flock >/dev/null 2>&1; then
        flock "$repo_lock" "$@"
        return
    fi
    python3 - "$repo_lock" "$@" <<'PY'
import fcntl
import subprocess
import sys

lock_path = sys.argv[1]
argv = sys.argv[2:]
with open(lock_path, "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    result = subprocess.run(argv)
raise SystemExit(result.returncode)
PY
}

# ---- optional per-agent claude-tap (host-side proxy the container routes to) --
# Tracing must NEVER fail an agent (mirrors peel_run.sh setup_tap / run.py wire-
# proxy philosophy): on ANY problem we warn and run that agent straight to
# api.anthropic.com. Each agent gets its OWN proxy + port + session so concurrent
# containers never share a tap thread. The proxy binds the docker BRIDGE GATEWAY
# IP (not 0.0.0.0) so it is reachable from the container via host.docker.internal
# without exposing the API proxy/dashboard on every host interface (codex 22:43/22:44).
discover_bridge_gw() {
    BRIDGE_GW="$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null | head -1)"
    [ -n "$BRIDGE_GW" ] || BRIDGE_GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
}

# Robustly stop a tap proxy + its child dashboard. The proxy pid in tap.pid spawns
# a `claude_tap dashboard` child; a bare `kill $proxy` can leave that child alive
# and make the next tapped launch hit port-busy/TAP_FAIL.
_kill_tap_proc() {  # $1 = proxy pid
    local pid="$1" kids p i
    if [ -n "$pid" ]; then
        kids="$(pgrep -P "$pid" 2>/dev/null || true)"
        kill -TERM $pid $kids 2>/dev/null || true
        for i in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
        for p in $pid $kids; do kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true; done
    fi
}

_kill_agent_tap() {  # $1 = agent_dir
    local ad="$1"
    if [ -f "$ad/tap.pid" ]; then
        _kill_tap_proc "$(cat "$ad/tap.pid" 2>/dev/null)"
        rm -f "$ad/tap.pid"
    fi
    return 0
}
# File-based, NOT array-based: start_agent_tap runs inside command substitution
# (launch_one and start_agent_tap are both `$(...)`), so an in-subshell TAP_PIDS
# append is invisible to the parent shell's trap (codex 22:55). The `tap.pid`
# FILES persist across subshells, so scan them — kills any proxy not yet reaped
# (reap()/DOCKER_FAIL remove their own tap.pid as they go).
_kill_all_taps() {
    local f ad
    for f in "$sweep_dir"/agent_*/tap.pid; do
        [ -f "$f" ] || continue
        ad="$(dirname "$f")"; _kill_agent_tap "$ad"
    done
}
_cleanup_provider_network() {
    [ "$PROVIDER_ONLY" = "1" ] || return 0
    if [ -n "$PROVIDER_PROXY_NAME" ]; then
        docker logs "$PROVIDER_PROXY_NAME" > "$sweep_dir/provider_proxy.log" 2>&1 || true
        docker rm -f "$PROVIDER_PROXY_NAME" >/dev/null 2>&1 || true
    fi
    if [ -n "$PROVIDER_NETWORK" ]; then
        docker network rm "$PROVIDER_NETWORK" >/dev/null 2>&1 || true
    fi
}
_finalize_profile_audit() {
    local agent_dir="$1" launcher_rc="${2:-143}"
    [ "$TRUSTED_CORE_PROFILE" = "1" ] || return 0
    [ ! -e "$agent_dir/usage_finalized" ] || return 0
    [ -f "$agent_dir/usage_started_at" ] \
        && [ -f "$agent_dir/usage_instance_id" ] \
        && [ -f "$agent_dir/usage_results_root" ] \
        && [ -f "$agent_dir/usage_project" ] || return 0
    local started instance results_root project ended
    started="$(cat "$agent_dir/usage_started_at")"
    instance="$(cat "$agent_dir/usage_instance_id")"
    results_root="$(cat "$agent_dir/usage_results_root")"
    project="$(cat "$agent_dir/usage_project")"
    ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    python3 "$repo/usage_audit.py" \
        --results-root "$results_root" --run-id "$RUN_ID" \
        --started-at "$started" --ended-at "$ended" \
        --launcher-rc "$launcher_rc" --project "$project" \
        --launch-instance-id "$instance" >/dev/null 2>&1 || true
    : > "$agent_dir/usage_finalized"
}
_cleanup_run_containers() {
    local name
    while IFS= read -r name; do
        case "$name" in
            "agent-$RUN_ID-"*) docker rm -f "$name" >/dev/null 2>&1 || true ;;
        esac
    done < <(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
}
_cleanup_all() {
    local launcher_rc="${1:-0}" agent_dir
    for agent_dir in "$sweep_dir"/agent_*; do
        [ -d "$agent_dir" ] || continue
        _finalize_profile_audit "$agent_dir" "$launcher_rc"
    done
    _cleanup_run_containers
    _kill_all_taps
    _cleanup_provider_network
}
# Cleanup runs once on EXIT. The signal traps re-exit so an INT/TERM/HUP can't
# fall through and continue the sweep into later agents (codex 23:00): each exit
# fires the EXIT trap, which does the single file-based cleanup.
trap '_cleanup_all "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# One-shot smoke: prove a container can resolve host.docker.internal before we
# burn real agents. Best-effort: a failure only warns.
tap_preflight() {
    [ "$TAP" = "1" ] || return 0
    discover_bridge_gw
    local bind="${TAP_HOST:-$BRIDGE_GW}"
    [ -n "$bind" ] || echo "tap: no bridge gateway IP + no DALEK_TAP_HOST — tap will be skipped per agent" >&2
    if docker run --rm --add-host host.docker.internal:host-gateway "$IMAGE" \
            sh -c 'getent hosts host.docker.internal >/dev/null 2>&1'; then
        echo "tap: host.docker.internal resolves from a container (bridge gw ${BRIDGE_GW:-unknown}, bind ${bind:-none})" >&2
    else
        echo "tap: WARNING host.docker.internal smoke FAILED — tapped agents may not reach the proxy" >&2
    fi
}

# start_agent_tap idx agent_dir -> echoes the host port on success (empty = no tap)
start_agent_tap() {
    local idx="$1" agent_dir="$2"
    [ "$TAP" = "1" ] || return 0
    local tap_bin; tap_bin="$(command -v claude-tap || echo "$HOME/.local/bin/claude-tap")"
    [ -x "$tap_bin" ] || { echo "tap: claude-tap not found — agent $idx WITHOUT tap" >&2; return 0; }
    local bind="${TAP_HOST:-$BRIDGE_GW}"
    [ -n "$bind" ] || { echo "tap: no bind addr — agent $idx WITHOUT tap" >&2; return 0; }
    local port=$((TAP_BASE_PORT + idx)) live=$((TAP_LIVE_BASE + idx))
    local p
    for p in "$port" "$live"; do
        if ss -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" \
                || lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "tap: port $p busy — agent $idx WITHOUT tap" >&2; return 0
        fi
    done
    mkdir -p "$TAP_OUT"
    nohup "$tap_bin" --tap-no-launch --tap-host "$bind" --tap-port "$port" \
        --tap-live --tap-live-port "$live" --tap-output-dir "$TAP_OUT" \
        --tap-store-stream-events --tap-no-update-check --tap-no-open \
        >> "$TAP_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$agent_dir/tap.pid"   # parent-visible across subshells (the trap scans these)
    printf 'port=%s\nlive_port=%s\nhost=%s\nbase_url=http://host.docker.internal:%s\ndashboard=http://%s:%s\n' \
        "$port" "$live" "$bind" "$port" "$bind" "$live" > "$agent_dir/tap.env"
    local i
    for i in $(seq 1 50); do
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "tap: agent $idx -> $bind:$port (dashboard :$live)" >&2
            echo "$port"; return 0
        fi
        sleep 0.2
    done
    echo "tap: proxy for agent $idx did not bind on $bind:$port — WITHOUT tap" >&2
    _kill_agent_tap "$agent_dir"; rm -f "$agent_dir/tap.env"
    return 0
}

# Scoreable-network primitive: agents attach only to an --internal network.
# A fixed-upstream reverse-proxy sidecar is dual-homed onto Docker's egress
# bridge. The agent can reach the sidecar by alias, but has no route to GitHub
# or any other public host; the sidecar ignores request authority and rejects
# CONNECT/non-provider paths.
start_provider_network() {
    [ "$PROVIDER_ONLY" = "1" ] || return 0
    PROVIDER_NETWORK="tc-${RUN_ID}"
    PROVIDER_PROXY_NAME="tc-provider-${RUN_ID}"
    docker network inspect "$PROVIDER_NETWORK" >/dev/null 2>&1 \
        && { echo "provider network already exists: $PROVIDER_NETWORK" >&2; return 1; }
    docker container inspect "$PROVIDER_PROXY_NAME" >/dev/null 2>&1 \
        && { echo "provider proxy container already exists: $PROVIDER_PROXY_NAME" >&2; return 1; }
    docker network create --internal "$PROVIDER_NETWORK" >/dev/null \
        || { echo "failed to create internal provider network" >&2; return 1; }
    [ "$(docker network inspect "$PROVIDER_NETWORK" --format '{{.Internal}}')" = "true" ] \
        || { echo "provider network was not created internal" >&2; return 1; }
    docker run -d --init --name "$PROVIDER_PROXY_NAME" \
        --network "$PROVIDER_NETWORK" --network-alias provider-proxy \
        "$IMAGE" \
        python3 /opt/harness/docker/provider_proxy.py \
            --policy /opt/harness/docker/provider_proxy_policy.json \
            --host 0.0.0.0 --port 8080 >/dev/null \
        || { echo "failed to start fixed provider proxy" >&2; return 1; }
    docker network connect bridge "$PROVIDER_PROXY_NAME" \
        || { echo "failed to attach provider proxy to egress bridge" >&2; return 1; }
    local proxy_networks
    proxy_networks="$(docker inspect "$PROVIDER_PROXY_NAME" \
        --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
    local proxy_network_count
    proxy_network_count="$(printf '%s\n' "$proxy_networks" | wc -w | xargs)"
    [ "$proxy_network_count" = "2" ] \
        && [[ " $proxy_networks " == *" $PROVIDER_NETWORK "* ]] \
        && [[ " $proxy_networks " == *" bridge "* ]] \
        || { echo "provider proxy is not dual-homed on only internal+bridge: $proxy_networks" >&2; return 1; }
    local i observed
    for i in $(seq 1 40); do
        observed="$(docker run --rm -i --network "$PROVIDER_NETWORK" "$IMAGE" \
            python3 - "$PROVIDER_POLICY_SHA256" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request
expected = sys.argv[1]
with urllib.request.urlopen("http://provider-proxy:8080/readyz", timeout=10) as response:
    value = json.load(response)
if value.get("okay") is True and value.get("policy_sha256") == expected:
    print(value["policy_sha256"])
PY
)"
        [ "$observed" = "$PROVIDER_POLICY_SHA256" ] && {
            echo "provider-only network ready: $PROVIDER_NETWORK policy=$observed" >&2
            return 0
        }
        sleep 0.5
    done
    echo "fixed provider proxy failed readiness/policy check" >&2
    return 1
}

# ---- launch one manifest as a detached container; echo the container name ----
launch_one() {
    local manifest="$1" pin="$2" depth="$3" minutes="$4" idx="$5"
    local agent_dir="$sweep_dir/agent_$idx"
    local host_wt="$agent_dir/peeled"      # peel worktree (shared store) — transient
    local work_vol="$agent_dir/work"       # isolated-store sealed tree -> /work
    local results="$agent_dir/results"
    local peel_json="$agent_dir/peel.json"
    mkdir -p "$agent_dir" "$results"
    local registration_for_leg="$LAUNCH_REGISTRATION"
    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        registration_for_leg="$agent_dir/launch_registration.json"
        cp "$LAUNCH_REGISTRATION" "$registration_for_leg"
        [ "$(sha256sum "$registration_for_leg" | awk '{print $1}')" = "$LAUNCH_REGISTRATION_SHA256" ] \
            || { echo "PROFILE REGISTRATION CHANGED idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_REGISTRATION_DRIFT"; return 1; }
        chmod a-w "$registration_for_leg"
    fi

    # Optional retry-memory seed. This is a prompt/context seed only: copy a
    # prior failure_memory.json into the new isolated /results before run.py
    # renders prompt_rendered.md. Each agent still writes its own private memory
    # file, so parallel runs do not race on a shared JSON.
    if [ -n "$FAILURE_MEMORY_SEED" ]; then
        cp "$FAILURE_MEMORY_SEED" "$results/failure_memory.json"
        sha256sum "$FAILURE_MEMORY_SEED" | awk '{print $1}' > "$agent_dir/failure_memory_seed.sha256"
        printf '%s\n' "$FAILURE_MEMORY_SEED" > "$agent_dir/failure_memory_seed.src"
        echo "FAILURE_MEMORY_SEED copied idx=$idx sha=$(cat "$agent_dir/failure_memory_seed.sha256") src=$FAILURE_MEMORY_SEED" >&2
    fi

    # 1) peel build — SERIALIZED (shared repo metadata mutation).
    local peel_args=( python3 "$repo/peel.py" --worktree "$host_wt"
                      --gitroot "$GITROOT" --ref "$REF"
                      --manifest "$manifest" --depth "$depth" )
    [ -n "$pin" ] && peel_args+=( --pin "$pin" )
    [ "$PROVIDER_ONLY" = "0" ] || peel_args+=( --require-exact-transform )
    # NOTE: launch_one's stdout is captured by the caller as the INFLIGHT record, so
    # every diagnostic here MUST go to stderr (>&2). Only the final `echo` of the
    # `name|idx|target_id|results` record is allowed on stdout.
    repo_locked "${peel_args[@]}" > "$peel_json" \
        || { echo "PEEL FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PEEL_FAIL"; return 1; }
    python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("okay") else 1)' "$peel_json" \
        || { echo "PEEL not okay idx=$idx" >&2; ledger_set "agent_$idx" "PEEL_FAIL"; return 1; }

    # 1b) optional OPERATOR seed (post-peel / pre-seal). This is launch
    #     scaffolding, not agent-authored proof progress: it becomes part of the
    #     sealed baseline and is recorded separately from guarded WIP resume
    #     patches. It may touch frozen source files, but only under the dalek
    #     source tree and only if it applies cleanly to the peeled tree.
    if [ -n "$OPERATOR_SEED" ]; then
        if ! python3 - "$OPERATOR_SEED" "$peel_json" >&2 <<'PY'; then
import json, re, sys
patch, pj = sys.argv[1], sys.argv[2]
editable = set(json.load(open(pj)).get("editable_files") or [])
touched = set()
for line in open(patch, errors="replace"):
    m = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
    if m:
        touched.add(m.group(2))
if not touched:
    print("operator seed guard: patch touches no files (not a unified git diff?)"); sys.exit(1)
bad = sorted(t for t in touched if not t.startswith("curve25519-dalek/src/"))
if bad:
    print("operator seed guard: patch touches non-source files: %s" % bad); sys.exit(1)
overlap = sorted(t for t in touched if t in editable)
if overlap:
    print("operator seed guard: note, seed also touches editable file(s): %s" % overlap)
print("operator seed guard OK: %d source file(s) touched" % len(touched)); sys.exit(0)
PY
            echo "OPERATOR_SEED guard FAIL idx=$idx" >&2; ledger_set "agent_$idx" "OPERATOR_SEED_GUARD_FAIL"; return 1
        fi
        if ! git -C "$host_wt" apply --check "$OPERATOR_SEED" 1>&2; then
            echo "OPERATOR_SEED apply --check FAIL idx=$idx (patch does not apply to peeled tree)" >&2
            ledger_set "agent_$idx" "OPERATOR_SEED_APPLY_FAIL"; return 1
        fi
        git -C "$host_wt" apply "$OPERATOR_SEED" 1>&2 \
            || { echo "OPERATOR_SEED apply FAIL idx=$idx" >&2; ledger_set "agent_$idx" "OPERATOR_SEED_APPLY_FAIL"; return 1; }
        cp "$OPERATOR_SEED" "$agent_dir/operator_seed.patch"
        sha256sum "$OPERATOR_SEED" | awk '{print $1}' > "$agent_dir/operator_seed.sha256"
        echo "OPERATOR_SEED applied idx=$idx sha=$(cat "$agent_dir/operator_seed.sha256") src=$OPERATOR_SEED" >&2
    fi

    # 1c) optional RESUME seed (post-peel / pre-seal). Apply a WIP diff into the
    #     freshly-peeled host_wt so the sealed /work starts from partially-
    #     reconstructed proofs instead of the bare peeled baseline. This is the
    #     correct seed point: seal_into_volume.sh copies working-tree bytes, so a
    #     pre-seal apply lands in the container; sealing a WIP *commit* then peeling
    #     would re-strip exactly the proof bodies we want to resume. GUARD: the
    #     patch may touch ONLY this manifest's editable_files (a peeled run can only
    #     legitimately edit those) and must apply cleanly; else fail the agent. The
    #     seed sha + patch copy are recorded in the agent dir for provenance.
    if [ -n "$SEED_WIP" ] && [ "$TRUSTED_CORE_PROFILE" = "0" ]; then
        if ! python3 - "$SEED_WIP" "$peel_json" >&2 <<'PY'; then
import json, re, sys
patch, pj = sys.argv[1], sys.argv[2]
editable = set(json.load(open(pj)).get("editable_files") or [])  # worktree-relative
touched = set()
for line in open(patch, errors="replace"):
    m = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
    if m:
        touched.add(m.group(2))
if not touched:
    print("seed guard: patch touches no files (not a unified git diff?)"); sys.exit(1)
bad = sorted(t for t in touched if t not in editable)
if bad:
    print("seed guard: patch touches non-editable files: %s" % bad); sys.exit(1)
print("seed guard OK: %d editable file(s) touched" % len(touched)); sys.exit(0)
PY
            echo "SEED guard FAIL idx=$idx" >&2; ledger_set "agent_$idx" "SEED_GUARD_FAIL"; return 1
        fi
        # launch_one's stdout is the captured INFLIGHT record, so ALL of git's
        # output (stdout AND stderr) must go to the launcher's stderr (fd2),
        # never to fd1. `1>&2` sends git's stdout to fd2; its stderr is already
        # fd2 (codex 21:19/21:23 — avoid the `2>&1 >&2` order bug that leaks).
        if ! git -C "$host_wt" apply --check "$SEED_WIP" 1>&2; then
            echo "SEED apply --check FAIL idx=$idx (patch does not apply to peeled tree)" >&2
            ledger_set "agent_$idx" "SEED_APPLY_FAIL"; return 1
        fi
        git -C "$host_wt" apply "$SEED_WIP" 1>&2 \
            || { echo "SEED apply FAIL idx=$idx" >&2; ledger_set "agent_$idx" "SEED_APPLY_FAIL"; return 1; }
        cp "$SEED_WIP" "$agent_dir/seed.patch"
        sha256sum "$SEED_WIP" | awk '{print $1}' > "$agent_dir/seed.sha256"
        echo "SEED applied idx=$idx sha=$(cat "$agent_dir/seed.sha256") src=$SEED_WIP" >&2

        # A source patch alone proves only that it applied. Bind the sealed
        # baseline to an ACCEPTED predecessor tree, or disclose that this is a
        # deliberately new seed before a container has any chance to run.
        local actual_seed_tree_hash=""
        actual_seed_tree_hash="$(python3 "$repo/lib/provenance.py" "$host_wt" --tree-hash)" \
            || { echo "SEED receipt hash FAIL idx=$idx" >&2; ledger_set "agent_$idx" "SEED_RECEIPT_FAIL"; return 1; }
        if [ -n "$SEED_RECEIPT_TREE_HASH" ] && [ "$actual_seed_tree_hash" != "$SEED_RECEIPT_TREE_HASH" ] \
                && [ -z "$SEED_OVERRIDE_REASON" ]; then
            echo "SEED receipt mismatch idx=$idx expected=$SEED_RECEIPT_TREE_HASH actual=$actual_seed_tree_hash" >&2
            ledger_set "agent_$idx" "SEED_RECEIPT_MISMATCH"; return 1
        fi
        if [ -z "$SEED_RECEIPT_TREE_HASH" ] && [ -z "$SEED_OVERRIDE_REASON" ]; then
            echo "SEED receipt missing idx=$idx" >&2
            ledger_set "agent_$idx" "SEED_RECEIPT_REQUIRED"; return 1
        fi
        if [ -n "$SEED_RECEIPT" ]; then
            cp "$SEED_RECEIPT" "$agent_dir/seed_promotion_receipt.json"
            sha256sum "$SEED_RECEIPT" | awk '{print $1}' > "$agent_dir/seed_promotion_receipt.sha256"
        fi
        python3 - "$agent_dir/seed_provenance.json" "$SEED_RECEIPT" \
                "$SEED_RECEIPT_TREE_HASH" "$actual_seed_tree_hash" "$SEED_OVERRIDE_REASON" <<'PY'
import json
import sys

path, receipt_path, expected, actual, override = sys.argv[1:]
json.dump({
    "schema_version": 1,
    "kind": "seed_provenance",
    "promotion_receipt_path": receipt_path or None,
    "expected_tree_hash": expected or None,
    "actual_tree_hash": actual,
    "exact_match": bool(expected) and expected == actual,
    "override_reason": override or None,
}, open(path, "w"), indent=2, sort_keys=True)
PY
        if [ -n "$SEED_OVERRIDE_REASON" ]; then
            echo "SEED override idx=$idx reason=$SEED_OVERRIDE_REASON expected=${SEED_RECEIPT_TREE_HASH:-none} actual=$actual_seed_tree_hash" >&2
        else
            echo "SEED receipt verified idx=$idx tree=$actual_seed_tree_hash" >&2
        fi
    fi

    # 2) seal the peeled tree into an isolated-store /work volume.
    #    seal_into_volume prints "SEAL OK ..." to stdout — redirect to stderr so it
    #    cannot contaminate the captured record (codex T112 16:40 blocker #1).
    bash "$here/seal_into_volume.sh" "$host_wt" "$work_vol" >&2 \
        || { echo "SEAL FAIL idx=$idx" >&2; ledger_set "agent_$idx" "SEAL_FAIL"; return 1; }
    # the shared-store peel worktree is no longer needed — drop it (and its store ref)
    repo_locked python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true

    # 3) derive container-relative paths. The anchor `target` lives in the MANIFEST,
    #    NOT in peel.py's --worktree summary (peel.py:350-361 has no "target") — so
    #    read it from the manifest, mirroring peel_run.sh:255 (codex T112 16:41
    #    ADDENDUM). member + experiment_mode come from peel.json. All are
    #    worktree-relative == /work-relative.
    local target_rel member_rel expmode
    target_rel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("target") or "")' "$manifest")"
    [ -n "$target_rel" ] || { echo "manifest has no target idx=$idx" >&2; ledger_set "agent_$idx" "NO_TARGET"; return 1; }
    read -r member_rel expmode < <(python3 - "$peel_json" <<'PY'
import json,sys,os
d=json.load(open(sys.argv[1]))
print(os.path.relpath(d["project"], d["worktree"]), d.get("experiment_mode") or "")
PY
)
    [ -n "$expmode" ] || { echo "no experiment_mode idx=$idx" >&2; ledger_set "agent_$idx" "NO_MODE"; return 1; }
    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        [ "$expmode" = "$PROFILE_EXPERIMENT_MODE" ] \
            || { echo "PROFILE MODE MISMATCH idx=$idx expected=$PROFILE_EXPERIMENT_MODE actual=$expmode" >&2; ledger_set "agent_$idx" "PROFILE_EXECUTION_MISMATCH"; return 1; }
        python3 - "$minutes" "$PROFILE_MINUTES" <<'PY' \
            || { echo "PROFILE MINUTES MISMATCH idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_EXECUTION_MISMATCH"; return 1; }
import sys
if float(sys.argv[1]) != float(sys.argv[2]):
    raise SystemExit(1)
PY
    fi
    local editable_rel=()
    while IFS= read -r p; do editable_rel+=("/work/$p"); done < <(
        python3 -c 'import json,sys; [print(p) for p in json.load(open(sys.argv[1]))["editable_files"]]' "$peel_json")
    local active_editable_rel=()
    while IFS= read -r p; do active_editable_rel+=("/work/$p"); done < <(
        python3 -c 'import json,sys; [print(p) for p in (json.load(open(sys.argv[1])).get("active_editable_files") or [])]' "$manifest")

    # A scored profile preserves the clean canonical root receipt before a
    # predecessor patch is replayed. The replay happens in the isolated store,
    # and the later lineage builder requires its full source tree to equal the
    # predecessor's reusable authority.
    local start_envelope="$agent_dir/start_envelope.json"
    local replayed_start="$agent_dir/replayed_start_envelope.json"
    local lineage_context="$results/_lineage_context.json"
    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        local prepared_start="$start_envelope"
        [ -z "$SEED_WIP" ] || prepared_start="$replayed_start"
        python3 "$repo/trusted_core_profile.py" prepare-start \
            --campaign "$CAMPAIGN_SPEC" --manifest "$manifest" \
            --peel-json "$peel_json" --project "$work_vol/$member_rel" \
            --repo-root "$repo" --out "$prepared_start" >/dev/null \
            || { echo "PROFILE START FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_START_FAIL"; return 1; }
        if [ -n "$SEED_WIP" ]; then
            python3 "$repo/trusted_core_profile.py" validate-root-replay \
                --authority "$ROOT_START_ENVELOPE" --replay "$replayed_start" \
                --out "$agent_dir/root_replay_validation.json" >/dev/null \
                || { echo "PROFILE ROOT REPLAY FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_ROOT_REPLAY_FAIL"; return 1; }
            cp "$ROOT_START_ENVELOPE" "$start_envelope"
            if ! python3 - "$SEED_WIP" "$peel_json" >&2 <<'PY'; then
import json, re, sys
patch, pj = sys.argv[1], sys.argv[2]
editable = set(json.load(open(pj)).get("editable_files") or [])
# Cargo may create the workspace lockfile during a scored leg even when the
# canonical source ref has none. Its bytes are safe to replay only here because
# the immediately following full-tree authority check must still match the
# immutable predecessor promotion receipt exactly.
editable.add("Cargo.lock")
touched = set()
for line in open(patch, errors="replace"):
    match = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
    if match:
        touched.add(match.group(2))
if not touched:
    print("profile seed guard: patch touches no files"); sys.exit(1)
bad = sorted(path for path in touched if path not in editable)
if bad:
    print("profile seed guard: non-editable files: %s" % bad); sys.exit(1)
print("profile seed guard OK: %d editable file(s)" % len(touched))
PY
                echo "PROFILE SEED GUARD FAIL idx=$idx" >&2
                ledger_set "agent_$idx" "PROFILE_SEED_GUARD_FAIL"; return 1
            fi
            git -C "$work_vol" apply --check "$SEED_WIP" 1>&2 \
                && git -C "$work_vol" apply "$SEED_WIP" 1>&2 \
                || { echo "PROFILE SEED APPLY FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_SEED_APPLY_FAIL"; return 1; }
            local actual_profile_seed_tree
            actual_profile_seed_tree="$(python3 "$repo/lib/provenance.py" "$work_vol/$member_rel" --tree-hash)" \
                || { echo "PROFILE SEED HASH FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_SEED_HASH_FAIL"; return 1; }
            [ "$actual_profile_seed_tree" = "$SEED_RECEIPT_TREE_HASH" ] \
                || { echo "PROFILE SEED AUTHORITY MISMATCH idx=$idx expected=$SEED_RECEIPT_TREE_HASH actual=$actual_profile_seed_tree" >&2; ledger_set "agent_$idx" "PROFILE_SEED_MISMATCH"; return 1; }
            cp "$SEED_WIP" "$agent_dir/seed.patch"
            cp "$SEED_RECEIPT" "$agent_dir/seed_promotion_receipt.json"
        fi
    fi

    # 4) assemble the in-container run.py argv (paths rooted at /work).
    local target_id; target_id="$(basename "$target_rel" .rs)"
    local active_editable_env=""
    if [ "${#active_editable_rel[@]}" -gt 0 ]; then
        active_editable_env="$(IFS=:; printf '%s' "${active_editable_rel[*]}")"
    fi
    local manifest_brief="" manifest_provenance="" manifest_pre_edit_block=""
    manifest_brief="$(python3 - "$manifest" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
lane = data.get("lane") or {}
brief = lane.get("operator_brief") or data.get("operator_brief") or ""
if brief:
    print(str(brief))
PY
)"
    manifest_provenance="$(python3 - "$manifest" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
lane = data.get("lane") or {}
provenance = lane.get("provenance") or data.get("provenance") or ""
if provenance:
    print(str(provenance))
PY
)"
    manifest_pre_edit_block="$(python3 - "$manifest" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    data = json.load(fh)
lane = data.get("lane") or {}
if data.get("pre_edit_diagnostic_block") or lane.get("pre_edit_diagnostic_block"):
    print("1")
PY
)"
    local effective_brief="${manifest_brief:-${DALEK_EXPERIMENT_BRIEF:-}}"
    local effective_provenance="${manifest_provenance:-${DALEK_EXPERIMENT_PROVENANCE:-}}"
    local effective_pre_edit_block="${manifest_pre_edit_block:-${DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK:-}}"
    if [ "$TRUSTED_CORE_PROFILE" = "1" ] \
            && { [ -n "$effective_brief" ] || [ -n "$effective_provenance" ] \
                || [ -n "$effective_pre_edit_block" ]; }; then
        echo "PROFILE H0 FAIL idx=$idx: unregistered operator hint/provenance channel" >&2
        ledger_set "agent_$idx" "PROFILE_HINT_MISMATCH"; return 1
    fi
    local cmd=( python3 /opt/harness/run.py "/work/$target_rel"
                --project "/work/$member_rel" --run-id "$RUN_ID"
                --rounds "$ROUNDS" --max-task-minutes "$minutes"
                --agent-max-turns "$AGENT_MAX_TURNS"
                --verus-rlimit "$VERUS_RLIMIT"
                --results /results
                --experiment-mode "$expmode"
                --experiment-allow-edit "${editable_rel[@]}" )
    [ "${#active_editable_rel[@]}" -eq 0 ] || \
        cmd+=( --experiment-active-edit "${active_editable_rel[@]}" )
    [ "$expmode" = "spec-proof" ] && cmd+=( --no-spec-gate )
    [ -n "$VSTD_CONTAINER" ] && cmd+=( --vstd-root "$VSTD_CONTAINER" )
    [ -n "$MODEL" ] && cmd+=( --model "$MODEL" )
    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        cmd+=( --lineage-context /results/_lineage_context.json
               --no-failure-memory --bank-partial )
    fi

    # 5) optional tap: start this agent's host-side proxy as LATE as possible
    #    (after peel/seal/seed cannot fail), immediately before docker run.
    local tap_port; tap_port="$(start_agent_tap "$idx" "$agent_dir")"

    # 5b) FAIL-CLOSED tap (--require-tap): every start_agent_tap failure path
    #     (missing binary / no bind addr / busy port / proxy didn't bind) yields an
    #     EMPTY tap_port, so an empty port here means tap did not engage. Abort the
    #     agent BEFORE docker run rather than silently running blind (the run006 bug).
    if [ "$REQUIRE_TAP" = "1" ] && [ -z "$tap_port" ]; then
        echo "TAP_FAIL idx=$idx: --require-tap set but tap proxy did not start (binary/bind/port)" >&2
        ledger_set "agent_$idx" "TAP_FAIL"; _kill_agent_tap "$agent_dir"; rm -f "$agent_dir/tap.env"
        return 1
    fi

    # 6) docker run — detached, CFS-shared CPU, per-container target via baked COW.
    local name="agent-$RUN_ID-$idx"
    # Run as the INVOKING host user (--user) so /work + /results are written with
    # host ownership — no chown/sudo needed (codex T112 16:40 blocker #3). The image
    # makes /opt/{cargo-home,cargo-target,agent-home} world-writable so an arbitrary
    # UID can still do its COW writes; HOME points at a writable baked dir.
    local docker_args=(
        run -d --init --name "$name"
        --user "$(id -u):$(id -g)"
        --cpu-shares "$CPU_SHARES" --memory "$MEM"
        -e "HOME=/opt/agent-home"
        -e "CARGO_NET_OFFLINE=true"
        -e "CARGO_HOME=/opt/cargo-home"
        -e "CARGO_TARGET_DIR=/opt/cargo-target"
        -e "CARGO_BUILD_JOBS=$CARGO_JOBS"
        -e "DALEK_AGENT_TARGET_PATH=/work/$target_rel"
        -v "$work_vol:/work"
        -v "$results:/results"
    )
    [ "$STERILITY_PREFLIGHT_ONLY" = "1" ] \
        || docker_args+=( -e "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN" )
    if [ "$PROVIDER_ONLY" = "1" ]; then
        docker_args+=(
            --network "$PROVIDER_NETWORK"
            -e "ANTHROPIC_BASE_URL=http://provider-proxy:8080"
            -e "DALEK_PROVIDER_POLICY_SHA256=$PROVIDER_POLICY_SHA256"
        )
    fi
    [ -z "$active_editable_env" ] || docker_args+=( -e "DALEK_AGENT_ACTIVE_EDIT_PATHS=$active_editable_env" )
    [ -n "$effective_provenance" ] \
        && docker_args+=( -e "DALEK_EXPERIMENT_PROVENANCE=$effective_provenance" )
    [ -n "$effective_brief" ] \
        && docker_args+=( -e "DALEK_EXPERIMENT_BRIEF=$effective_brief" )
    [ -n "$effective_pre_edit_block" ] \
        && docker_args+=( -e "DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK=$effective_pre_edit_block" )
    [ -n "$REGISTRY_RO" ] && docker_args+=( -v "$REGISTRY_RO:/opt/cargo-home/registry:ro" )
    # Route the container's claude through the host tap proxy (host.docker.internal
    # = the bridge gateway via --add-host). Only when start_agent_tap succeeded.
    if [ -n "$tap_port" ]; then
        docker_args+=( --add-host host.docker.internal:host-gateway
                       -e "ANTHROPIC_BASE_URL=http://host.docker.internal:$tap_port" )
    fi

    # Before the model can run, exercise the exact image, sealed work tree,
    # result mount, internal network, and fixed proxy. A failed or missing
    # receipt aborts the agent; there is no disclosure-only fallback.
    if [ "$PROVIDER_ONLY" = "1" ]; then
        local sterility="$agent_dir/sterility_receipt.json"
        if ! docker run --rm --init \
                --network "$PROVIDER_NETWORK" \
                --user "$(id -u):$(id -g)" \
                -e "HOME=/opt/agent-home" \
                -v "$work_vol:/work" -v "$results:/results" \
                "$IMAGE" python3 /opt/harness/docker/sterility_probe.py \
                    --work /work --proxy-host provider-proxy --proxy-port 8080 \
                    --policy-sha256 "$PROVIDER_POLICY_SHA256" > "$sterility"; then
            echo "STERILITY FAIL idx=$idx" >&2
            ledger_set "agent_$idx" "STERILITY_FAIL"
            return 1
        fi
        python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("okay") else 1)' "$sterility" \
            || { echo "STERILITY receipt not okay idx=$idx" >&2; ledger_set "agent_$idx" "STERILITY_FAIL"; return 1; }
        local image_harness_tree
        image_harness_tree="$(docker run --rm --init \
            --network "$PROVIDER_NETWORK" "$IMAGE" \
            python3 /opt/harness/trusted_core_profile.py harness-receipt \
                --repo-root /opt/harness \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["tree_hash"])')" \
            || { echo "IMAGE HARNESS RECEIPT FAIL idx=$idx" >&2; ledger_set "agent_$idx" "STERILITY_FAIL"; return 1; }
        python3 - "$sterility" "$agent_dir/sterility_envelope.json" \
                "$IMAGE_DIGEST" "$PROVIDER_POLICY_SHA256" "$PROVIDER_NETWORK" \
                "$PROVIDER_PROXY_NAME" "$work_vol" "$results" \
                "$image_harness_tree" <<'PY'
import hashlib
import json
import os
import sys

probe_path, out_path, image, policy, network, proxy, work, results, harness_tree = sys.argv[1:]
probe_bytes = open(probe_path, "rb").read()
value = {
    "schema_version": 1,
    "kind": "sterility_envelope",
    "okay": True,
    "container_image_digest": image,
    "provider_proxy_policy_sha256": policy,
    "harness_source_tree_sha256": harness_tree,
    "internal_network": network,
    "provider_proxy_container": proxy,
    "agent_mounts": [
        {"source": os.path.realpath(work), "destination": "/work"},
        {"source": os.path.realpath(results), "destination": "/results"},
    ],
    "in_container_probe_sha256": hashlib.sha256(probe_bytes).hexdigest(),
    "in_container_probe": json.loads(probe_bytes),
}
canonical = json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
).encode()
value["receipt_id"] = hashlib.sha256(canonical).hexdigest()
payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(payload)
PY
        if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
            local context_args=(
                python3 "$repo/trusted_core_profile.py" prepare-context
                --campaign "$CAMPAIGN_SPEC" --manifest "$manifest"
                --start "$start_envelope" --project "$work_vol/$member_rel"
                --repo-root "$repo" --registration "$registration_for_leg"
                --sterility "$agent_dir/sterility_envelope.json"
                --out "$lineage_context"
            )
            if [ -n "$SEED_RECEIPT" ]; then
                context_args+=(
                    --predecessor "$SEED_RECEIPT"
                    --predecessor-terminal "$PREDECESSOR_TERMINAL"
                    --campaign-state "$CAMPAIGN_STATE"
                )
            fi
            "${context_args[@]}" >/dev/null \
                || { echo "PROFILE CONTEXT FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_CONTEXT_FAIL"; return 1; }
            if [ -n "$CAMPAIGN_STATE" ]; then
                python3 - "$CAMPAIGN_STATE" "$lineage_context" <<'PY' \
                    || { echo "PROFILE CAMPAIGN STATE FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_STATE_FAIL"; return 1; }
import hashlib
import json
import sys
state = json.load(open(sys.argv[1]))
context = json.load(open(sys.argv[2]))
payload = dict(state)
payload.pop("receipt_id", None)
payload.pop("receipt_path", None)
observed = hashlib.sha256(json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
).encode()).hexdigest()
if state.get("receipt_id") != observed:
    raise SystemExit("campaign state content ID mismatch")
if state.get("kind") != "trusted_core_campaign_state":
    raise SystemExit("campaign state kind mismatch")
if state.get("stop") is True:
    raise SystemExit("campaign state already requires stop")
if state.get("lineage_id") != context.get("lineage_id"):
    raise SystemExit("campaign state lineage mismatch")
PY
            fi
        fi
        if [ "$STERILITY_PREFLIGHT_ONLY" = "1" ]; then
            ledger_set "$target_id" "STERILITY_OK"
            echo "PREFLIGHT_OK|$idx|$target_id|$results"
            return 0
        fi
    fi

    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        local usage_started usage_instance
        usage_started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        usage_instance="${RUN_ID}.agent-${idx}"
        printf '%s\n' "$usage_started" > "$agent_dir/usage_started_at"
        printf '%s\n' "$usage_instance" > "$agent_dir/usage_instance_id"
        printf '%s\n' "$results" > "$agent_dir/usage_results_root"
        printf '%s\n' "$work_vol/$member_rel" > "$agent_dir/usage_project"
        python3 "$repo/usage_audit.py" \
            --results-root "$results" --run-id "$RUN_ID" \
            --started-at "$usage_started" --project "$work_vol/$member_rel" \
            --launch-instance-id "$usage_instance" >/dev/null \
            || { echo "PROFILE USAGE INIT FAIL idx=$idx" >&2; ledger_set "agent_$idx" "PROFILE_USAGE_INIT_FAIL"; return 1; }
    fi

    docker "${docker_args[@]}" "$IMAGE" "${cmd[@]}" >/dev/null \
        || { echo "DOCKER RUN FAIL idx=$idx" >&2; ledger_set "agent_$idx" "DOCKER_FAIL"; _kill_agent_tap "$agent_dir"; return 1; }

    # 6b) FAIL-CLOSED tap post-launch assertion (--require-tap): the container must
    #     have BOTH a written tap.env AND the routing env var actually present in its
    #     namespace. If either is missing the agent would talk straight to the API
    #     untapped — kill it + its proxy and fail the agent rather than run blind.
    if [ "$REQUIRE_TAP" = "1" ]; then
        local want="ANTHROPIC_BASE_URL=http://host.docker.internal:$tap_port"
        if [ ! -f "$agent_dir/tap.env" ] \
                || ! docker exec "$name" env 2>/dev/null | grep -qF "$want"; then
            echo "TAP_FAIL idx=$idx: post-launch tap verification failed (tap.env or ANTHROPIC_BASE_URL missing)" >&2
            docker rm -f "$name" >/dev/null 2>&1 || true
            _kill_agent_tap "$agent_dir"; ledger_set "agent_$idx" "TAP_FAIL"
            return 1
        fi
    fi
    if [ "$PROVIDER_ONLY" = "1" ]; then
        local networks
        networks="$(docker inspect "$name" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
        if [ "$networks" != "$PROVIDER_NETWORK " ]; then
            echo "STERILITY FAIL idx=$idx: agent networks=$networks expected=$PROVIDER_NETWORK" >&2
            docker rm -f "$name" >/dev/null 2>&1 || true
            ledger_set "agent_$idx" "STERILITY_FAIL"
            return 1
        fi
    fi
    echo "$name|$idx|$target_id|$results|$work_vol|$member_rel"
}

# ---- reap a finished container: classify rc, update ledger -------------------
# run.py exit 42 == RATE_LIMITED -> break the whole sweep (every later target
# would be rejected too until the window reopens), mirroring launch.sh rc-42.
RATE_LIMITED=0
TAP_FAILED=0          # set when a launch_one fails under --require-tap -> sweep exits 43
LAUNCH_FAILED=0       # provider-only path: any missing receipt/launch aborts scoreability
PROFILE_FAILED=0      # scored-profile terminal/accounting validation failed
reap() {
    local name="$1" idx="$2" target_id="$3" results="$4" work_vol="${5:-}" member_rel="${6:-}"
    local rc; rc=$(docker wait "$name")
    docker logs "$name" > "$sweep_dir/agent_$idx.log" 2>&1 || true
    _kill_agent_tap "$sweep_dir/agent_$idx"   # stop this agent's tap proxy (best-effort)
    local result_json="$results/$RUN_ID/$target_id/result.json"
    local end_reason="UNKNOWN"
    [ -f "$result_json" ] && end_reason=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("end_reason","UNKNOWN"))' "$result_json" 2>/dev/null || echo UNKNOWN)
    echo "MARKER idx=$idx target=$target_id rc=$rc end_reason=$end_reason"
    if [ "$TRUSTED_CORE_PROFILE" = "1" ]; then
        _finalize_profile_audit "$sweep_dir/agent_$idx" "$rc"
        local audit="$results/$RUN_ID/usage_audit.json"
        local terminal="$sweep_dir/agent_$idx/terminal_validation.json"
        local state="$sweep_dir/agent_$idx/campaign_state.json"
        # A provider 429 is intentionally non-reusable, so reusable-terminal
        # validation must not run first and shadow RATE_LIMITED as exit 45.
        # Still fail closed unless the result and complete usage audit agree
        # that this was a loss-free, fully accounted rate-limit boundary.
        if [ "$rc" = "42" ] || [ "$end_reason" = "RATE_LIMITED" ]; then
            if [ ! -f "$result_json" ] || [ ! -f "$audit" ] \
                    || ! python3 - "$result_json" "$audit" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
audit = json.load(open(sys.argv[2], encoding="utf-8"))
if result.get("end_reason") != "RATE_LIMITED":
    raise SystemExit(1)
if audit.get("cost_status") != "complete":
    raise SystemExit(1)
if (audit.get("counts") or {}).get("unresolved_streams") != 0:
    raise SystemExit(1)
PY
            then
                echo "PROFILE RATE_LIMIT AUDIT FAIL idx=$idx end_reason=$end_reason" >&2
                ledger_set "$target_id" "PROFILE_RATE_LIMIT_AUDIT_FAIL"
                PROFILE_FAILED=1
            else
                ledger_set "$target_id" "RATE_LIMITED"
                RATE_LIMITED=1
            fi
        elif [ ! -f "$result_json" ] || [ ! -f "$audit" ] \
                || ! python3 "$repo/trusted_core_profile.py" validate-terminal \
                    --context "$results/_lineage_context.json" \
                    --result "$result_json" --usage-audit "$audit" \
                    --project "$work_vol/$member_rel" --out "$terminal" \
                    --max-cost-usd "$PROFILE_MAX_COST_USD" \
                    --max-wall-seconds "$PROFILE_MAX_WALL_SECONDS" >/dev/null; then
            echo "PROFILE TERMINAL FAIL idx=$idx end_reason=$end_reason" >&2
            ledger_set "$target_id" "PROFILE_TERMINAL_FAIL"
            PROFILE_FAILED=1
        else
            local state_args=(
                python3 "$repo/trusted_core_profile.py" advance-state
                --terminal "$terminal" --result "$result_json" --out "$state"
                --plateau-k "$PROFILE_PLATEAU_K"
                --max-cost-usd "$PROFILE_MAX_COST_USD"
                --max-wall-seconds "$PROFILE_MAX_WALL_SECONDS"
            )
            [ -z "$CAMPAIGN_STATE" ] || state_args+=( --prior "$CAMPAIGN_STATE" )
            if ! "${state_args[@]}" >/dev/null; then
                echo "PROFILE STATE ADVANCE FAIL idx=$idx" >&2
                ledger_set "$target_id" "PROFILE_STATE_FAIL"
                PROFILE_FAILED=1
            else
                local decision
                decision="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["decision"])' "$terminal")"
                ledger_set "$target_id" "$decision"
            fi
        fi
    elif [ "$rc" = "42" ] || [ "$end_reason" = "RATE_LIMITED" ]; then
        ledger_set "$target_id" "RATE_LIMITED"; RATE_LIMITED=1
    elif [ "$end_reason" = "COMPLETE" ]; then
        ledger_set "$target_id" "success"
    else
        ledger_set "$target_id" "$end_reason"
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
}

# ---- offline dependency/toolchain preflight gate -----------------------------
# A scored profile MUST prove that the exact image can resolve dependencies and
# enter `cargo verus` while offline before any model process starts. A shared
# read-only registry remains only an optimization: ordinary runs may clear it
# and fall back to a proven baked CARGO_HOME, but trusted-core runs fail closed.
run_registry_preflight() {
    local required=0
    [ "$TRUSTED_CORE_PROFILE" = "0" ] || required=1
    [ -n "$REGISTRY_RO" ] || [ "$required" = "1" ] || return 0
    local first_manifest
    first_manifest=$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$MANIFESTS_FILE" | xargs 2>/dev/null || true)
    if [ ! -f "$first_manifest" ]; then
        [ "$required" = "0" ] \
            || die "trusted-core offline preflight has no sample manifest"
        echo "preflight: no sample manifest — disabling REGISTRY_RO" >&2
        REGISTRY_RO=""
        return 0
    fi
    local pdir="$sweep_dir/_preflight" host_wt work_vol pj
    rm -rf "$pdir"; mkdir -p "$pdir"
    host_wt="$pdir/peeled"; work_vol="$pdir/work"; pj="$pdir/peel.json"
    if ! repo_locked python3 "$repo/peel.py" --worktree "$host_wt" \
            --gitroot "$GITROOT" --ref "$REF" --manifest "$first_manifest" --depth 1 > "$pj" 2>&2; then
        [ "$required" = "0" ] \
            || die "trusted-core offline preflight sample peel failed"
        echo "preflight: sample peel failed — disabling REGISTRY_RO" >&2
        REGISTRY_RO=""
        return 0
    fi
    if ! bash "$here/seal_into_volume.sh" "$host_wt" "$work_vol" >&2; then
        repo_locked python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true
        [ "$required" = "0" ] \
            || die "trusted-core offline preflight sample seal failed"
        echo "preflight: sample seal failed — disabling REGISTRY_RO" >&2
        REGISTRY_RO=""
        return 0
    fi
    local member; member=$(python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); print(os.path.relpath(d["project"],d["worktree"]))' "$pj")
    local cache_args=()
    [ -z "$REGISTRY_RO" ] \
        || cache_args+=( -v "$REGISTRY_RO:/opt/cargo-home/registry:ro" )
    echo "preflight: validating exact-image offline cache on /work/$member ..." >&2
    if docker run --rm --init --user "$(id -u):$(id -g)" -e "HOME=/opt/agent-home" \
            -e "CARGO_NET_OFFLINE=true" -e "CARGO_HOME=/opt/cargo-home" -e "CARGO_TARGET_DIR=/opt/cargo-target" \
            -e "CARGO_BUILD_JOBS=$CARGO_JOBS" \
            -v "$work_vol:/work" "${cache_args[@]}" \
            "$IMAGE" bash /opt/harness/docker/preflight.sh "/work/$member" >&2; then
        echo "preflight: exact-image offline dependency/toolchain check OK" >&2
    else
        repo_locked python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true
        rm -rf "$work_vol"
        [ "$required" = "0" ] \
            || die "trusted-core exact-image offline dependency/toolchain check failed"
        echo "preflight: FAILED — clearing REGISTRY_RO and using the baked CARGO_HOME" >&2
        REGISTRY_RO=""
        return 0
    fi
    repo_locked python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true
    rm -rf "$work_vol"
}
run_registry_preflight
tap_preflight            # --tap only: discover bridge gw + smoke host.docker.internal
if ! start_provider_network; then
    echo "SWEEP FAILED — provider-only network/proxy boundary incomplete (exit 44)" >&2
    exit 44
fi

# ---- sweep loop with a concurrency cap (shared CPU, bounded thread supply) ----
declare -a INFLIGHT=()   # entries: name|idx|target_id|results|work_vol|member_rel
idx=0
while IFS= read -r raw || [ -n "$raw" ]; do
    line="${raw%%#*}"; line="$(echo "$line" | xargs 2>/dev/null || true)"
    [ -n "$line" ] || continue
    IFS='|' read -r manifest pin depth minutes <<<"$line"
    manifest="$(echo "$manifest" | xargs)"; pin="$(echo "${pin:-}" | xargs)"
    depth="$(echo "${depth:-1}" | xargs)"; minutes="$(echo "${minutes:-180}" | xargs)"
    [ -f "$manifest" ] || die "manifest not found: $manifest"
    target_id_guess="$(python3 -c 'import json,sys; t=json.load(open(sys.argv[1])).get("target") or ""; import os; print(os.path.basename(t)[:-3] if t.endswith(".rs") else t)' "$manifest" 2>/dev/null || true)"

    if [ "$SKIP_EXISTING" = "1" ] && [ -n "$target_id_guess" ] && [ "$(ledger_status "$target_id_guess")" = "success" ]; then
        echo "SKIP (already success): $target_id_guess"; continue
    fi
    [ "$RATE_LIMITED" = "1" ] && { echo "RATE_LIMITED — halting sweep"; break; }

    # block until a slot frees
    while [ "${#INFLIGHT[@]}" -ge "$MAX_PARALLEL" ]; do
        # reap the first that has exited (poll); keep it simple and robust.
        new=()
        for e in "${INFLIGHT[@]}"; do
            IFS='|' read -r n i t r w m <<<"$e"
            if [ -z "$(docker ps -q -f name="^${n}\$")" ]; then reap "$n" "$i" "$t" "$r" "$w" "$m"; else new+=("$e"); fi
        done
        INFLIGHT=("${new[@]}")
        [ "${#INFLIGHT[@]}" -ge "$MAX_PARALLEL" ] && sleep 5
        [ "$RATE_LIMITED" = "1" ] && break
    done
    [ "$RATE_LIMITED" = "1" ] && { echo "RATE_LIMITED — halting sweep"; break; }

    if entry=$(launch_one "$manifest" "$pin" "$depth" "$minutes" "$idx"); then
        if [[ "$entry" == PREFLIGHT_OK\|* ]]; then
            echo "$entry"
        else
            INFLIGHT+=("$entry"); echo "LAUNCHED $entry"
        fi
    elif [ "$REQUIRE_TAP" = "1" ]; then
        # FAIL-CLOSED: a launch_one failure under --require-tap (TAP_FAIL) must NOT be
        # silently dropped — otherwise the sweep can still exit 0 and an operator/wrapper
        # mistakes a tap-less/aborted launch for success. Halt: drain what's already
        # running, then exit 43.
        echo "TAP_FAIL — halting sweep (--require-tap): agent idx=$idx did not launch tapped" >&2
        TAP_FAILED=1; break
    elif [ "$PROVIDER_ONLY" = "1" ]; then
        echo "PROVIDER_ONLY_FAIL — halting sweep: agent idx=$idx did not produce a complete pre-model boundary" >&2
        LAUNCH_FAILED=1; break
    fi
    idx=$((idx+1))
done < "$MANIFESTS_FILE"

# drain
if [ "${#INFLIGHT[@]}" -gt 0 ]; then
    for e in "${INFLIGHT[@]}"; do
        IFS='|' read -r n i t r w m <<<"$e"
        reap "$n" "$i" "$t" "$r" "$w" "$m"
    done
fi
echo "SWEEP DONE run-id=$RUN_ID ledger=$ledger"
if [ "$RATE_LIMITED" = "1" ]; then exit 42; fi
if [ "$TAP_FAILED" = "1" ]; then echo "SWEEP FAILED — TAP_FAIL under --require-tap (exit 43)" >&2; exit 43; fi
if [ "$LAUNCH_FAILED" = "1" ]; then echo "SWEEP FAILED — provider-only boundary incomplete (exit 44)" >&2; exit 44; fi
if [ "$PROFILE_FAILED" = "1" ]; then echo "SWEEP FAILED — trusted-core terminal validation incomplete (exit 45)" >&2; exit 45; fi
exit 0
