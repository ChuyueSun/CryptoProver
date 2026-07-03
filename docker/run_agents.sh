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
# RESUME (--seed-wip <patch>): apply a WIP unified git diff into each peeled
# host worktree AFTER peel, BEFORE seal, so the container resumes from partially-
# reconstructed proofs. The patch is GUARDED to touch only the manifest's
# editable_files and must apply cleanly. Use a SEPARATE --run-id/--work-base from
# the clean sweep (the host ledger keys status by target_id, so a same-target
# clean+resume in one sweep would clobber each other). Typical:
#   git -C <peeled-repo> diff <baseline> <wip-ref> > /tmp/wip.patch
#   docker/run_agents.sh ... --run-id resume_001 --seed-wip /tmp/wip.patch
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
MAX_PARALLEL="$(nproc 2>/dev/null || echo 4)"
CARGO_JOBS=4                     # CARGO_BUILD_JOBS: compile fan-out per container
REGISTRY_RO=""                   # optional host registry cache; only after preflight
SKIP_EXISTING=0
REQUIRE_TAP=0                    # --require-tap: fail-closed tap — TAP_FAIL + abort if tap doesn't engage
VSTD_CONTAINER="${DALEK_VSTD_CONTAINER:-}"   # baked vstd path in image, optional
MODEL=""                         # optional Claude model forwarded to run.py

die() { echo "run_agents: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --gitroot) GITROOT="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --manifests-file) MANIFESTS_FILE="$2"; shift 2 ;;
        --seed-wip) SEED_WIP="$2"; shift 2 ;;
        --failure-memory-seed) FAILURE_MEMORY_SEED="$2"; shift 2 ;;
        --operator-seed) OPERATOR_SEED="$2"; shift 2 ;;
        --tap) TAP=1; shift ;;
        --no-tap) TAP=0; shift ;;
        --require-tap) TAP=1; REQUIRE_TAP=1; shift ;;   # fail-closed: abort the agent if tap can't engage
        --work-base) WORK_BASE="$2"; shift 2 ;;
        --cpu-shares) CPU_SHARES="$2"; shift 2 ;;
        --mem) MEM="$2"; shift 2 ;;
        --rounds) ROUNDS="$2"; shift 2 ;;
        --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
        --cargo-jobs) CARGO_JOBS="$2"; shift 2 ;;
        --registry-ro) REGISTRY_RO="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --skip-existing) SKIP_EXISTING=1; shift ;;
        *) die "unknown arg: $1" ;;
    esac
done
[ -n "$GITROOT" ] || die "--gitroot required"
[ -n "$RUN_ID" ] || die "--run-id required"
[ -n "$MANIFESTS_FILE" ] || die "--manifests-file required"
[ -z "$SEED_WIP" ] || [ -f "$SEED_WIP" ] || die "--seed-wip patch not found: $SEED_WIP"
[ -z "$FAILURE_MEMORY_SEED" ] || [ -f "$FAILURE_MEMORY_SEED" ] || die "--failure-memory-seed not found: $FAILURE_MEMORY_SEED"
[ -z "$OPERATOR_SEED" ] || [ -f "$OPERATOR_SEED" ] || die "--operator-seed patch not found: $OPERATOR_SEED"
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || die "CLAUDE_CODE_OAUTH_TOKEN not set (memory:run-claude-auth)"
command -v docker >/dev/null || die "docker not on PATH"
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
mkdir -p "$sweep_dir"
[ -f "$ledger" ] || echo '{}' > "$ledger"

ledger_status() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],{}).get("status",""))' "$ledger" "$1"; }
ledger_set() {    python3 - "$ledger" "$1" "$2" <<'PY'
import json,sys
p,k,v=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(p)); d[k]={"status":v}; json.dump(d,open(p,"w"),indent=2)
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
    [ -f "$ad/tap.pid" ] && { _kill_tap_proc "$(cat "$ad/tap.pid" 2>/dev/null)"; rm -f "$ad/tap.pid"; }
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
# Cleanup runs once on EXIT. The signal traps re-exit so an INT/TERM/HUP can't
# fall through and continue the sweep into later agents (codex 23:00): each exit
# fires the EXIT trap, which does the single file-based cleanup.
trap '_kill_all_taps' EXIT
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

# ---- launch one manifest as a detached container; echo the container name ----
launch_one() {
    local manifest="$1" pin="$2" depth="$3" minutes="$4" idx="$5"
    local agent_dir="$sweep_dir/agent_$idx"
    local host_wt="$agent_dir/peeled"      # peel worktree (shared store) — transient
    local work_vol="$agent_dir/work"       # isolated-store sealed tree -> /work
    local results="$agent_dir/results"
    local peel_json="$agent_dir/peel.json"
    mkdir -p "$agent_dir" "$results"

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
    # NOTE: launch_one's stdout is captured by the caller as the INFLIGHT record, so
    # every diagnostic here MUST go to stderr (>&2). Only the final `echo` of the
    # `name|idx|target_id|results` record is allowed on stdout.
    flock "$repo_lock" "${peel_args[@]}" > "$peel_json" \
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
    if [ -n "$SEED_WIP" ]; then
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
    fi

    # 2) seal the peeled tree into an isolated-store /work volume.
    #    seal_into_volume prints "SEAL OK ..." to stdout — redirect to stderr so it
    #    cannot contaminate the captured record (codex T112 16:40 blocker #1).
    bash "$here/seal_into_volume.sh" "$host_wt" "$work_vol" >&2 \
        || { echo "SEAL FAIL idx=$idx" >&2; ledger_set "agent_$idx" "SEAL_FAIL"; return 1; }
    # the shared-store peel worktree is no longer needed — drop it (and its store ref)
    flock "$repo_lock" python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true

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
    local editable_rel=()
    while IFS= read -r p; do editable_rel+=("/work/$p"); done < <(
        python3 -c 'import json,sys; [print(p) for p in json.load(open(sys.argv[1]))["editable_files"]]' "$peel_json")
    local active_editable_rel=()
    while IFS= read -r p; do active_editable_rel+=("/work/$p"); done < <(
        python3 -c 'import json,sys; [print(p) for p in (json.load(open(sys.argv[1])).get("active_editable_files") or [])]' "$manifest")

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
    local cmd=( python3 /opt/harness/run.py "/work/$target_rel"
                --project "/work/$member_rel" --run-id "$RUN_ID"
                --rounds "$ROUNDS" --max-task-minutes "$minutes"
                --results /results
                --experiment-mode "$expmode"
                --experiment-allow-edit "${editable_rel[@]}" )
    [ "${#active_editable_rel[@]}" -eq 0 ] || \
        cmd+=( --experiment-active-edit "${active_editable_rel[@]}" )
    [ "$expmode" = "spec-proof" ] && cmd+=( --no-spec-gate )
    [ -n "$VSTD_CONTAINER" ] && cmd+=( --vstd-root "$VSTD_CONTAINER" )
    [ -n "$MODEL" ] && cmd+=( --model "$MODEL" )

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
        -e "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN"
        -e "CARGO_NET_OFFLINE=true"
        -e "CARGO_HOME=/opt/cargo-home"
        -e "CARGO_TARGET_DIR=/opt/cargo-target"
        -e "CARGO_BUILD_JOBS=$CARGO_JOBS"
        -e "DALEK_AGENT_TARGET_PATH=/work/$target_rel"
        -v "$work_vol:/work"
        -v "$results:/results"
    )
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
    echo "$name|$idx|$target_id|$results"
}

# ---- reap a finished container: classify rc, update ledger -------------------
# run.py exit 42 == RATE_LIMITED -> break the whole sweep (every later target
# would be rejected too until the window reopens), mirroring launch.sh rc-42.
RATE_LIMITED=0
TAP_FAILED=0          # set when a launch_one fails under --require-tap -> sweep exits 43
reap() {
    local name="$1" idx="$2" target_id="$3" results="$4"
    local rc; rc=$(docker wait "$name")
    docker logs "$name" > "$sweep_dir/agent_$idx.log" 2>&1 || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    _kill_agent_tap "$sweep_dir/agent_$idx"   # stop this agent's tap proxy (best-effort)
    local result_json="$results/$RUN_ID/$target_id/result.json"
    local end_reason="UNKNOWN"
    [ -f "$result_json" ] && end_reason=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("end_reason","UNKNOWN"))' "$result_json" 2>/dev/null || echo UNKNOWN)
    echo "MARKER idx=$idx target=$target_id rc=$rc end_reason=$end_reason"
    if [ "$rc" = "42" ] || [ "$end_reason" = "RATE_LIMITED" ]; then
        ledger_set "$target_id" "RATE_LIMITED"; RATE_LIMITED=1
    elif [ "$end_reason" = "COMPLETE" ]; then
        ledger_set "$target_id" "success"
    else
        ledger_set "$target_id" "$end_reason"
    fi
}

# ---- registry-ro preflight gate (codex T112 16:47 #2) -----------------------
# The ro shared-registry mount is an OPTIMIZATION, not a correctness assumption. Do
# NOT mount it per-agent until ONE sample container proves the offline cache + the ro
# mount actually drive a bounded `cargo verus verify` under the exact image/env. On
# failure we CLEAR REGISTRY_RO (global) and fall back to the baked per-container
# CARGO_HOME — every agent is still correct, just uses more disk.
run_registry_preflight() {
    [ -n "$REGISTRY_RO" ] || return 0
    local first_manifest
    first_manifest=$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$MANIFESTS_FILE" | xargs 2>/dev/null || true)
    [ -f "$first_manifest" ] || { echo "preflight: no sample manifest — disabling REGISTRY_RO" >&2; REGISTRY_RO=""; return 0; }
    local pdir="$sweep_dir/_preflight" host_wt work_vol pj
    rm -rf "$pdir"; mkdir -p "$pdir"
    host_wt="$pdir/peeled"; work_vol="$pdir/work"; pj="$pdir/peel.json"
    if ! flock "$repo_lock" python3 "$repo/peel.py" --worktree "$host_wt" \
            --gitroot "$GITROOT" --ref "$REF" --manifest "$first_manifest" --depth 1 > "$pj" 2>&2; then
        echo "preflight: sample peel failed — disabling REGISTRY_RO" >&2; REGISTRY_RO=""; return 0
    fi
    if ! bash "$here/seal_into_volume.sh" "$host_wt" "$work_vol" >&2; then
        echo "preflight: sample seal failed — disabling REGISTRY_RO" >&2; REGISTRY_RO=""
        flock "$repo_lock" python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true
        return 0
    fi
    local member; member=$(python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); print(os.path.relpath(d["project"],d["worktree"]))' "$pj")
    echo "preflight: validating offline cache + ro registry on /work/$member ..." >&2
    if docker run --rm --init --user "$(id -u):$(id -g)" -e "HOME=/opt/agent-home" \
            -e "CARGO_NET_OFFLINE=true" -e "CARGO_HOME=/opt/cargo-home" -e "CARGO_TARGET_DIR=/opt/cargo-target" \
            -e "CARGO_BUILD_JOBS=$CARGO_JOBS" \
            -v "$work_vol:/work" -v "$REGISTRY_RO:/opt/cargo-home/registry:ro" \
            "$IMAGE" bash /opt/harness/docker/preflight.sh "/work/$member" >&2; then
        echo "preflight: OK — keeping ro registry mount" >&2
    else
        echo "preflight: FAILED — clearing REGISTRY_RO, falling back to baked per-container CARGO_HOME" >&2
        REGISTRY_RO=""
    fi
    flock "$repo_lock" python3 "$repo/peel.py" --worktree "$host_wt" --gitroot "$GITROOT" --remove >/dev/null 2>&1 || true
    rm -rf "$work_vol"
}
run_registry_preflight
tap_preflight            # --tap only: discover bridge gw + smoke host.docker.internal

# ---- sweep loop with a concurrency cap (shared CPU, bounded thread supply) ----
declare -a INFLIGHT=()   # entries: name|idx|target_id|results
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
            IFS='|' read -r n i t r <<<"$e"
            if [ -z "$(docker ps -q -f name="^${n}\$")" ]; then reap "$n" "$i" "$t" "$r"; else new+=("$e"); fi
        done
        INFLIGHT=("${new[@]}")
        [ "${#INFLIGHT[@]}" -ge "$MAX_PARALLEL" ] && sleep 5
        [ "$RATE_LIMITED" = "1" ] && break
    done
    [ "$RATE_LIMITED" = "1" ] && { echo "RATE_LIMITED — halting sweep"; break; }

    if entry=$(launch_one "$manifest" "$pin" "$depth" "$minutes" "$idx"); then
        INFLIGHT+=("$entry"); echo "LAUNCHED $entry"
    elif [ "$REQUIRE_TAP" = "1" ]; then
        # FAIL-CLOSED: a launch_one failure under --require-tap (TAP_FAIL) must NOT be
        # silently dropped — otherwise the sweep can still exit 0 and an operator/wrapper
        # mistakes a tap-less/aborted launch for success. Halt: drain what's already
        # running, then exit 43.
        echo "TAP_FAIL — halting sweep (--require-tap): agent idx=$idx did not launch tapped" >&2
        TAP_FAILED=1; break
    fi
    idx=$((idx+1))
done < "$MANIFESTS_FILE"

# drain
for e in "${INFLIGHT[@]}"; do IFS='|' read -r n i t r <<<"$e"; reap "$n" "$i" "$t" "$r"; done
echo "SWEEP DONE run-id=$RUN_ID ledger=$ledger"
if [ "$RATE_LIMITED" = "1" ]; then exit 42; fi
if [ "$TAP_FAILED" = "1" ]; then echo "SWEEP FAILED — TAP_FAIL under --require-tap (exit 43)" >&2; exit 43; fi
exit 0
