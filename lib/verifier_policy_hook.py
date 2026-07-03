#!/usr/bin/env python3
"""PreToolUse Bash hook: hard-block verifier commands that the prompt forbids.

Rationale (internal review, T-crosstalk): `prompt.md` already tells the agent to run
verifier skills in the FOREGROUND (no `run_in_background`), with no shell
`timeout` wrapper, no fixed `/tmp/*.json` verifier files, no broad
`pkill`/`killall`/`pgrep -f`, no merged-stderr JSON parser, no verifier
output slicing, no raw admit-count greps, and no raw `cargo verus | grep/head`
substitutions — but two independent runs (gcp13 bare-metal, e2e1 docker)
*ignored* the rule and died on the post-round `PROCESS_CROSSTALK` gate after
burning budget. This hook turns the soft prompt rule into a hard block AT the
tool call: it denies the offending Bash tool_use and feeds the model the
corrective instruction immediately, so the round never reaches the crosstalk
gate. The post-round detector (`run.py:detect_process_crosstalk`) stays as the
audit backstop.

Claude Code PreToolUse contract: the event JSON arrives on stdin
(`tool_name`, `tool_input`); exit code 2 with a message on stderr BLOCKS the
tool call and returns the stderr text to the model. Exit 0 allows it.

Patterns mirror `run.py`'s gate regexes (kept in sync intentionally) plus the
`cargo verus | grep/head` substitution codex flagged.
"""
import json
import os
import re
import subprocess
import sys

# Mirror run.py's _VERIFIER_PROCESS_RE / _SHELL_TIMEOUT_RE / _SHARED_TMP_OUTPUT_RE
# / _BROAD_PROCESS_CONTROL_RE so the hook and the post-round detector agree.
_VERIFIER = re.compile(r"\b(?:verus_check\.py|cargo\s+verus|cargo-verus|rust_verify|z3)\b")
_TIMEOUT = re.compile(r"(?:^|[;&|]\s*)timeout\s+(?:-\S+\s+)*\d")
# fixed /tmp verifier output, but allow the harness's own /tmp/{dalek-,claude-,codex-}* scratch
_SHARED_TMP = re.compile(r"(?:^|[\s>|])/tmp/(?!dalek-|claude-|codex-)[A-Za-z0-9_.-]+\.(?:json|out|log|err)\b")
_BROAD_PROC = re.compile(r"\b(?:pkill|killall|pgrep\s+-f)\b")
# Any raw `cargo verus` / `cargo-verus` invocation. prompt.md:78-81 forbids
# substituting direct cargo-verus for verus_check.py at all (it can forward
# module filters into vstd/dependency crates and report misleading errors),
# not only the `| grep/head`-truncated form. Block the whole class.
_CARGO_RAW = re.compile(r"\bcargo(?:-|\s+)verus\b")
# Direct `verus ...` invocations bypass the harness wrapper in the same way as
# raw cargo-verus. Keep this command-token based so comments/paths mentioning
# "verus" do not trip it.
_DIRECT_VERUS_RAW = re.compile(
    r"(?:^|[;&|]\s*)(?:timeout\s+(?:-\S+\s+)*\d+\s+)?(?:\S*/)?verus(?:\s|$)"
)
# raw cargo-verus piped into grep/head — the truncating substitution; reported
# with a more specific reason when it matches.
_CARGO_GREP = re.compile(r"cargo(?:-|\s+)verus\b.*\|\s*(?:grep|head)\b")
_MERGED_STDERR_JSON = re.compile(
    r"(?:2\s*>\s*&\s*1|\|&)[\s\S]*json\s*\.\s*load"
)
_HARNESS_SKILL = re.compile(
    r"\b(?:verus_check|spec_check|admit_inventory|search_semantic|search_module|"
    r"search_macro|search_proven)\.py\b"
)
_VERIFIER_OUTPUT_SLICE = re.compile(r"\|\s*(?:head|tail|grep)\b")
_VERUS_CHECK_HELP = re.compile(r"\bverus_check\.py\b[\s\S]*?(?:^|\s)(?:-h|--help)(?:\s|$)")
_RAW_ADMIT_COUNT_FLAG = re.compile(r"\b(?:grep|rg)\b[\s\S]*\s(?:-[A-Za-z]*c[A-Za-z]*|--count)\b")
_RAW_ADMIT_COUNT_PIPE = re.compile(r"\|\s*wc\s+-l\b")


def _is_verus_check_help_only(cmd):
    """Allow sliced `verus_check.py --help`; it is CLI help, not verifier truth."""
    segments = [s for s in re.split(r"[;&]", cmd) if _VERIFIER.search(s)]
    return bool(segments) and all(
        _VERUS_CHECK_HELP.search(segment) and not _CARGO_RAW.search(segment)
        and not _DIRECT_VERUS_RAW.search(segment)
        for segment in segments
    )


def _is_raw_admit_count(cmd):
    """Block misleading raw admit counts; allow line searches and admit_inventory."""
    if "admit_inventory.py" in cmd or "admit" not in cmd:
        return False
    return bool(_RAW_ADMIT_COUNT_PIPE.search(cmd) or _RAW_ADMIT_COUNT_FLAG.search(cmd))

# Absolute path to this harness's verus_check skill, derived from __file__ so the
# corrective message hands the model a real absolute path (prompt.md:66-70 requires
# absolute skill paths even after `cd` into the Cargo project root).
_HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERUS_CHECK = os.path.join(_HARNESS_ROOT, "skills", "verus_check.py")


def _split_paths(value):
    return [p for p in (value or "").split(os.pathsep) if p]


def _path_has_git_diff(project_root, path):
    """Return True when `path` has an unstaged/staged source diff.

    Fail open on git/path errors: this hook is a tactical pre-edit guard, not a
    replacement for the post-round integrity gates.
    """
    if not project_root or not path:
        return True
    try:
        proc = subprocess.run(
            ["git", "-C", project_root, "diff", "--quiet", "--", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        return True
    if proc.returncode == 1:
        return True
    if proc.returncode == 0:
        return False
    return True


def _pre_edit_guard_active():
    return os.environ.get("DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK", "").strip() == "1"


def _pre_edit_guard_has_diff():
    paths = _split_paths(os.environ.get("DALEK_AGENT_ACTIVE_EDIT_PATHS", ""))
    if not paths:
        target = os.environ.get("DALEK_AGENT_TARGET_PATH", "").strip()
        if target:
            paths = [target]
    if not paths:
        return True
    project_root = os.environ.get("DALEK_AGENT_PROJECT_ROOT", "").strip()
    return any(_path_has_git_diff(project_root, path) for path in paths)

_MSG = (
    "BLOCKED by proof-harness policy ({reasons}). Per prompt.md: run "
    "`python3 {verus_check} <target> --project <root>` in the FOREGROUND "
    "and read its JSON stdout directly. Use `verus_check.py --timeout N` (and "
    "`--rlimit N`) instead of a shell `timeout` wrapper; do NOT use "
    "run_in_background, do NOT redirect verifier output to /tmp/*.json|out|log, "
    "do NOT pipe merged stderr into a JSON parser, "
    "do NOT pipe verifier output through head/tail/grep, "
    "do NOT use raw grep/rg counts for admits (use admit_inventory.py over the "
    "full editable scope), "
    "do NOT pkill/killall/pgrep -f, and never substitute raw `cargo verus` "
    "(or `cargo verus | grep/head`) for verus_check.py. The harness owns this "
    "round's timeout + cleanup."
)

_PRE_EDIT_MSG = (
    "BLOCKED by proof-harness policy ({reasons}). This run has an operator "
    "pre-edit proof-thread guard: the starting verifier/search signal is known, "
    "so diagnostic skills are blocked until the active source file has a git "
    "diff. Read the active file if needed, then make the requested source edit "
    "in the active proof thread. After that diff exists, rerun the scoped check "
    "normally."
)


def evaluate(tool_name, tool_input):
    """Return a list of violation reasons (empty = allow). Pure for testing."""
    if tool_name != "Bash":
        return []
    cmd = (tool_input or {}).get("command") or ""
    if not isinstance(cmd, str) or not cmd:
        return []
    bg = bool((tool_input or {}).get("run_in_background"))
    reasons = []
    direct_verus = bool(_DIRECT_VERUS_RAW.search(cmd))
    is_verifier = bool(_VERIFIER.search(cmd) or direct_verus) and not _is_verus_check_help_only(cmd)
    is_harness_skill = bool(_HARNESS_SKILL.search(cmd))
    if _pre_edit_guard_active() and (is_verifier or is_harness_skill) and not _pre_edit_guard_has_diff():
        reasons.append("pre-edit proof-thread diagnostic before active source diff")
    if bg and is_verifier:
        reasons.append("run_in_background verifier")
    if is_verifier and _TIMEOUT.search(cmd):
        reasons.append("shell timeout-wrapped verifier")
    if is_verifier and _SHARED_TMP.search(cmd):
        reasons.append("verifier output to shared /tmp/*.{json,out,log,err}")
    if (is_verifier or is_harness_skill) and _MERGED_STDERR_JSON.search(cmd):
        reasons.append("merged stderr into harness-skill JSON parser")
    if is_verifier and _VERIFIER_OUTPUT_SLICE.search(cmd):
        reasons.append("verifier output piped through head/tail/grep")
    if _is_raw_admit_count(cmd):
        reasons.append("raw admit count (use admit_inventory.py)")
    if _BROAD_PROC.search(cmd):
        reasons.append("broad process control (pkill/killall/pgrep -f)")
    if _CARGO_RAW.search(cmd):
        if _CARGO_GREP.search(cmd):
            reasons.append("raw cargo-verus | grep/head substitution (truncates errors)")
        else:
            reasons.append("raw cargo-verus substitution (use verus_check.py)")
    if _DIRECT_VERUS_RAW.search(cmd):
        reasons.append("raw direct verus substitution (use verus_check.py)")
    return reasons


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        # Never block on a malformed event — fail open (the post-round gate backstops).
        sys.exit(0)
    reasons = evaluate(event.get("tool_name"), event.get("tool_input"))
    if reasons:
        if any(r.startswith("pre-edit proof-thread") for r in reasons):
            sys.stderr.write(_PRE_EDIT_MSG.format(reasons=", ".join(reasons)) + "\n")
            sys.exit(2)
        sys.stderr.write(
            _MSG.format(reasons=", ".join(reasons), verus_check=_VERUS_CHECK) + "\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
