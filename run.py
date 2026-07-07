#!/usr/bin/env python3
"""Dalek-Lite MVP driver.

Loop:
    claude -p --verbose --output-format stream-json <prompt>
    round:
        - save raw NDJSON
        - parse END_REASON from result text
        - run spec_check verify (gate: any drift = failed round)
        - run verus_check (source of truth: verus_okay)
        - record round_N.json
    continue with `claude -c` until COMPLETE | LIMIT | NEEDS_DECOMP | max_rounds

NEEDS_DECOMP is an escalation: the agent declares the proof is blocked on
missing infrastructure (a helper lemma/chain that doesn't exist, or a
sub-lemma split) rather than grinding to the time limit. The loop breaks on
it, the label is preserved into result.json / failure_memory, and a fresh
run_task on the same target gives the retry +2 rounds, 1.5x wall-clock, and a
"build the named infrastructure first" directive.

Usage:
    python run.py <target.rs> [--project <cargo_root>] [--rounds 5]
                              [--run-id <id>] [--results <dir>]
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).parent))
from lib import discovery_brief, failure_memory, results  # noqa: E402
from lib.admits import count_non_axiom as _count_llm_target_admits  # noqa: E402
from lib.admits import find_matching_brace, find_proof_fn_body_brace  # noqa: E402
from lib.admits import axiom_fn_names  # noqa: E402
from lib.admits import count_forbidden_constructs  # noqa: E402
from lib.admits import strip_comments_strings  # noqa: E402
from lib.results import RoundResult, TaskResult, task_dir, write_json  # noqa: E402


HERE = Path(__file__).parent.resolve()
PROMPT_TEMPLATE = HERE / "prompt.md"
_DEFAULT_VERUS_RLIMIT = 80.0

def _make_agent_cwd(label: str = "") -> Path:
    """Create a fresh per-task scratch cwd for the claude subprocess and return
    it (call ONCE per task, then reuse for every round). Used as the claude
    subprocess cwd so Claude Code does not inject HERE's CLAUDE.md into the proof
    agent's context: Claude Code auto-loads CLAUDE.md by walking UP the cwd
    ancestry and injects it into EVERY request as a `# claudeMd`
    <system-reminder> block. HERE's CLAUDE.md is the harness operator/dev doc
    (~7.5k tokens) — pure noise for the agent, which only needs the rendered
    prompt + skills. So we launch from a dir OUTSIDE the repo that symlinks
    `skills/` + `lib/`, keeping legacy relative `python skills/<name>.py` /
    `Read skills/SKILL.md` calls working if the agent stays in this cwd. The
    rendered prompt uses absolute skill paths so skill invocations also keep
    working after the agent `cd`s into the Cargo project root.

    A FRESH per-task tempdir (tempfile.mkdtemp), not a shared global path: one
    shared dir is a footgun under the documented parallel-worktree fan-out — two
    runs from different checkouts would flap each other's symlinks mid-round, so
    an agent could execute the OTHER checkout's skill code. mkdtemp gives each
    task its own collision-free dir; reusing it across the task's rounds keeps
    the cwd's session-project slug stable so `--resume` finds the session.

    On ANY setup failure, fall back to HERE (the old, proven cwd) with a LOUD
    warning rather than silently returning a dir missing the skills link — older
    prompts and stale round-history may still contain relative `python
    skills/<name>.py` commands, and those would otherwise 404 and silently
    degrade the task (the harness's own absolute-path gates still run, so it
    burns budget without false-greening)."""
    try:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:60]
        cwd = Path(tempfile.mkdtemp(prefix=f"dalek_agent_cwd_{slug}_"))
        for name in ("skills", "lib"):
            (cwd / name).symlink_to(HERE / name, target_is_directory=True)
        atexit.register(shutil.rmtree, cwd, ignore_errors=True)
        return cwd
    except OSError as e:
        print(f"[run] WARNING: could not build agent scratch cwd ({e}); "
              f"falling back to cwd=HERE — the harness CLAUDE.md (~7.5k tokens) "
              f"will be injected into agent context for this task.", flush=True)
        return HERE


def _write_agent_bash_env(out_dir: Path, project: Path) -> Path:
    """Write the Bash startup hook used by Claude Code's Bash tool.

    Claude itself still runs from `_make_agent_cwd()` so its CLAUDE.md discovery
    ancestry stays outside this harness repo. The Bash tool, however, should
    begin each noninteractive shell in the Cargo project root; otherwise agents
    repeatedly burn turns re-learning `cd /work/... && ...`, and relative
    source-path probes resolve under the scratch cwd. Bash sources `$BASH_ENV`
    for noninteractive `bash -c` commands, which gives us that project-root
    starting directory without moving the parent Claude process into the repo.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agent_bash_env.sh"
    quoted_project = shlex.quote(str(project.resolve()))
    if path.exists():
        path.chmod(0o644)
    path.write_text(
        "# dalek-lite harness: start Claude Bash tool calls in the Cargo project root.\n"
        f"_dalek_agent_project_root={quoted_project}\n"
        'if [ -d "$_dalek_agent_project_root" ]; then\n'
        '    cd "$_dalek_agent_project_root" || exit $?\n'
        "else\n"
        '    echo "[dalek-harness] project root missing: $_dalek_agent_project_root" >&2\n'
        "    exit 111\n"
        "fi\n"
        "unset _dalek_agent_project_root\n",
        encoding="utf-8",
    )
    path.chmod(0o444)
    return path


# Conditionally-injected guidance: the "Decompose hard admits" section. Only
# carried in the prompt for targets that actually have a hard function (see
# `target_needs_decompose`); empty otherwise, to keep the eager prompt lean.
DECOMPOSE_TEMPLATE = HERE / "prompt_decompose.md"

# Scope the spawned proof agent's toolset to only file + shell tools. The
# dalek-lite proof CLIs are run via Bash, and the skill reference is read on
# demand from `skills/SKILL.md` (a plain file). Everything else is stripped to
# keep the system prompt lean and off unrelated capabilities.
#
# `--tools` is the flag that actually filters tool *availability*; it also
# excludes MCP tools (they aren't in the built-in set). `--allowedTools` is
# only a *permission* allowlist and is a no-op under
# `--permission-mode bypassPermissions`, so it does NOT shrink the toolset.
# `--strict-mcp-config` (with no `--mcp-config`) deterministically loads zero
# MCP servers so connected ones (Gmail/Calendar/Drive) never leak in.
# `--disable-slash-commands` drops ALL discovered skills/slash-commands. We do
# NOT use native skills: enabling the `Skill` tool exposed 14 skills (1 project +
# 13 inherited user-global/built-in noise) with no flag to scope to just ours,
# and in a real run the native skill never fired — the lean prompt index carried
# the proof round. So: zero skill noise, and the agent `Read`s `skills/SKILL.md`
# for exact flags when it needs them.
# `Task` and `Agent` are two aliases for the SAME subagent tool, and `--tools`
# matches either. (Verified live on 2.1.128: `--tools Bash,Task` and
# `--tools Bash,Agent` both retain the subagent tool; `--tools Bash` alone drops
# it.) Keep both aliases OUT of the proof-agent toolset: headless subagents are
# write-capable and share the same worktree, process namespace, and scratch
# locations. The gcp5/corefloor traces include subagents editing files outside
# the parent editable set and broad process/tmp crosstalk, which defeats the
# harness's frozen-file recovery model. Grep/Glob are intentionally absent: this
# Claude Code build doesn't expose them as separate tools, and the agent greps
# via Bash (prompt.md already says "or raw grep"). `TodoWrite` is also absent in
# the noninteractive proof-agent context even when listed in `--tools` (the raw
# stream reports "TodoWrite exists but is not enabled"), so the prompt asks for
# a plain-text checklist instead.
AGENT_TOOL_FLAGS = [
    "--tools", "Bash,Read,Edit,Write",
    "--strict-mcp-config",
    "--disable-slash-commands",
]


def _write_agent_settings(dest_dir: Path) -> Path:
    """Write a `claude --settings` file installing the PreToolUse verifier-policy
    hook (`lib/verifier_policy_hook.py`).

    The hook HARD-BLOCKS the verifier Bash patterns `prompt.md` already forbids
    (run_in_background verifier, shell `timeout`-wrapped verifier, verifier
    output to fixed `/tmp/*.{json,out,log,err}`, broad pkill/killall/pgrep -f,
    raw `cargo verus | grep/head`) at the tool call, so the round never reaches
    — and never burns budget up to — the post-round `detect_process_crosstalk`
    gate (which stays as the audit backstop). Returns the settings path.
    """
    hook = Path(__file__).resolve().parent / "lib" / "verifier_policy_hook.py"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command",
                         "command": f"{sys.executable} {hook}"}
                    ],
                }
            ]
        }
    }
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "agent_settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path
END_REASON_RE = re.compile(
    r"(?m)^\s*END_REASON:(COMPLETE|LIMIT|NEEDS_DECOMP|FALSE_CONTRACT)\s*$", re.I)


# ----------------- helpers -----------------

# Match a `proof fn axiom_*` header. Admits inside such functions are
# axioms-by-convention (e.g. precomputed-table validity, primality, etc.)
# that cannot be discharged by SMT and are intentionally left as `admit()`.
# They must NOT count toward the "admits remaining" gate, or those files
# will be permanently LIMITed.
def _rejection_continue_msg(verus_okay: bool, admits_left: int) -> str:
    """Build the continuation message when a previous round's
    `END_REASON:COMPLETE` is overridden by the harness's final-state
    gate. Pure function so it can be unit-tested; the loop below
    prepends its output to the round-history block on the next round."""
    if not verus_okay and admits_left == 0:
        return (
            f"Your previous END_REASON:COMPLETE was rejected: "
            f"verus_okay={verus_okay}, non-axiom admits remaining="
            f"{admits_left}. There are no remaining admits to hunt; COMPLETE "
            f"is blocked by Verus errors. Re-run `python3 /opt/harness/skills/verus_check.py "
            f"<absolute-file.rs> --project <project-root>`, fix the listed "
            f"whole-crate/module diagnostics in any editable target, sibling, "
            f"or top-level API proof body allowed by this prompt (API contracts "
            f"remain frozen), then declare COMPLETE again — or emit "
            f"END_REASON:LIMIT if you cannot."
        )
    return (
        f"Your previous END_REASON:COMPLETE was rejected: "
        f"verus_okay={verus_okay}, non-axiom admits remaining="
        f"{admits_left}. Re-run `python3 /opt/harness/skills/verus_check.py "
        f"<absolute-file.rs> --project <project-root>`, locate any remaining "
        f"`admit()` outside `proof fn axiom_*` bodies, fix them, "
        f"then declare COMPLETE again — or emit END_REASON:LIMIT "
        f"if you cannot."
    )


def _sibling_failure_continue_msg(files: list[str]) -> str:
    """Continuation nudge for an unresolved sibling/top-level verify failure.

    Sibling failures are actionable compile/proof errors, not proof-bypass
    cheats. They must block COMPLETE, but while rounds/budget remain the agent
    should see the diagnostics and repair them instead of the harness dying.
    """
    shown = ", ".join(files[:5]) + (", ..." if len(files) > 5 else "")
    return (
        "Sibling re-verification failed after your previous edits. This is not "
        "a terminal harness error while budget remains: fix the sibling/top-level "
        f"diagnostics below before declaring COMPLETE. Failing file(s): {shown}."
    )


def _frozen_edit_continue_msg(files: list[str]) -> str:
    """Continuation nudge after the agent edited a frozen (non-editable) file.

    A misplaced helper lemma is legitimate work in an illegal location, not a
    proof-bypass cheat (the cheat vectors — weakened specs / contracts — are
    caught terminally by SPEC_DRIFT). So while budget remains the harness
    reverts the frozen file to baseline and asks the agent to RELOCATE the work
    into an editable file, instead of killing the run on the first misplacement.
    Pure function so it can be unit-tested; the loop reverts + prepends this.
    """
    shown = ", ".join(files[:5]) + (", ..." if len(files) > 5 else "")
    return (
        "Your previous round edited FROZEN (non-editable) file(s), which the "
        f"harness has REVERTED to baseline: {shown}. These belong to the frozen "
        "floor — editing them can never count and fails the round as "
        "FROZEN_EDIT. If you added helper lemmas there, RELOCATE them: define "
        "each as a NEW top-level `proof fn lemma_*` in an editable file that "
        "needs it (e.g. the editable target or API proof file itself — new "
        "lemma contracts in editable files are allowed; only the original API "
        "contracts and the frozen floor are immutable). Do NOT edit the listed "
        "file(s) again; re-derive from the frozen specs and lemmas only. "
        "Repeating this exhausts the retry budget and fails the task."
    )


def _spec_drift_label(drift: dict) -> str:
    file_name = Path(str(drift.get("file") or "")).name or "<unknown-file>"
    fn = str(drift.get("function") or drift.get("key") or "<unknown-fn>")
    field = str(drift.get("field") or drift.get("change") or "modified")
    return f"{file_name}::{fn}.{field}"


def _spec_drift_continue_msg(drift: list[dict], restored_files: list[str]) -> str:
    """Continuation nudge after frozen specs were restored."""
    shown = ", ".join(_spec_drift_label(d) for d in drift[:5])
    if len(drift) > 5:
        shown += ", ..."
    files = ", ".join(restored_files[:5]) + (", ..." if len(restored_files) > 5 else "")
    return (
        "Your previous round modified frozen spec surface, which the harness "
        f"has restored before continuing. Drifted item(s): {shown}. "
        f"Restored file(s): {files}. Do not edit frozen spec headers, "
        "requires/ensures/decreases clauses, verifier attributes, or existing "
        "spec-function bodies. Keep proving against the restored contracts. "
        "Repeating the same spec drift exhausts the retry budget and fails the task."
    )


def _flatten_sibling_fail_messages(sibling_fail: list[dict]) -> list[dict]:
    """Turn sibling re-verify failures into normal Verus diagnostics.

    `round_N.json` is the feedback source for the next round. If sibling
    diagnostics stay only in stdout, the agent can be stopped by a sibling
    failure without ever seeing the actionable file/line in its continuation
    prompt.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for item in sibling_fail:
        context_file = str(item.get("file") or "")
        errors = item.get("errors") or []
        if not errors:
            errors = [{
                "file": context_file,
                "line": 0,
                "column": 0,
                "data": "sibling re-verify failed",
            }]
        for err in errors:
            if isinstance(err, dict):
                msg = dict(err)
            else:
                msg = {"file": context_file, "line": 0, "column": 0,
                       "data": str(err)}
            msg.setdefault("file", context_file)
            msg.setdefault("line", 0)
            msg.setdefault("column", 0)
            data = str(msg.get("data", ""))
            if context_file:
                data = f"sibling re-verify failed for {context_file}: {data}"
            else:
                data = f"sibling re-verify failed: {data}"
            msg["data"] = data
            key = (msg.get("file"), msg.get("line"), msg.get("column"), data)
            if key in seen:
                continue
            seen.add(key)
            out.append(msg)
    return out


def _final_end_reason(done_for_real: bool, loop_end_reason: Optional[str]) -> str:
    """Resolve the task's recorded end_reason from the final-state gate.

    Pure function so the decision table can be unit-tested; the loop below
    just feeds it `done_for_real` (verus okay AND no hard admits remain) and
    the agent's self-declared `loop_end_reason`.

    Priority:
      1. RATE_LIMITED / RATE_LIMIT_OR_HANG / RETRY_EXHAUSTED / TRANSPORT_ERROR /
         USER_INTERRUPTED / INTERRUPTED_SIGNAL / PROCESS_CROSSTALK ⇒ preserved, ABOVE
         the done_for_real promotion. These mean the round never really ran;
         even a trivial (zero-hard-admit) target that verus accepts must NOT be
         promoted to COMPLETE off it — otherwise the infrastructure failure is
         masked and the launcher won't halt. Recording the label keeps it out
         of proven_registry so a later --skip-existing re-run picks it back up
         honestly.
      2. `done_for_real` ⇒ COMPLETE, regardless of what the agent claimed.
         Promotes an over-cautious LIMIT (only intentional axioms left) and a
         NEEDS_DECOMP the agent actually discharged before escalating.
      3. NEEDS_DECOMP ⇒ preserved (Feature2). A distinct, machine-countable
         "needs missing infrastructure" escalation — not silently flattened
         into LIMIT, so a retry can detect it and bump its budget.
      4. anything else ⇒ LIMIT (COMPLETE claimed but evidence disagrees, or
         an honest LIMIT).

    Exception (highest priority): a cheating signal — SPEC_DRIFT (a frozen
    spec was weakened), AXIOM_DRIFT (a new `proof fn axiom_*` was injected),
    TOOLING_DRIFT (the agent edited the harness's own verification skills
    under skills/ + lib/), or FORBIDDEN_CONSTRUCT (a new `assume(...)` or
    `#[verifier::external_body]` was introduced), or PROCESS_CROSSTALK (the
    agent used shared process/tmp controls) — is NEVER promoted to
    COMPLETE, even when verus is green and no hard admits remain. Weakening a
    spec / injecting an axiom / doctoring a verification skill / assuming the
    goal / corrupting shared process state is *precisely* how an agent makes
    verus pass without a real proof, so a green final state is not evidence of
    done — it's evidence the contamination worked."""
    lr = (loop_end_reason or "").upper()
    if lr in ("RATE_LIMITED", "RATE_LIMIT_OR_HANG", "RETRY_EXHAUSTED", "TRANSPORT_ERROR",
              "USER_INTERRUPTED", "INTERRUPTED_SIGNAL", "PROCESS_CROSSTALK"):
        return lr
    if lr in ("SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT", "SIBLING_VERUS_FAIL",
              "GIT_RECOVERY", "FROZEN_EDIT", "FORBIDDEN_CONSTRUCT"):
        # Terminal: never promoted to COMPLETE even when the target locally
        # verifies. The cheat-class drifts make verus pass without a real
        # proof; SIBLING_VERUS_FAIL means a sibling/top-level module the agent
        # touched no longer verifies — a target-only green is not done.
        # GIT_RECOVERY means the agent copied the answer out of git history.
        return lr
    if lr == "FALSE_CONTRACT":
        # Honest escalation (NOT a cheat), but terminal and preserved even over a
        # green: a machine-verified-false frozen contract means the crate cannot
        # be honestly completed (the false lemma is unprovable), so it must never
        # read as COMPLETE. Only set after run.py verified the agent's witness.
        return "FALSE_CONTRACT"
    if done_for_real:
        return "COMPLETE"
    if lr == "NEEDS_DECOMP":
        return "NEEDS_DECOMP"
    return "LIMIT"


_TRACE_TAINTED_RETRY_MEMORY_REASONS = frozenset({
    "GIT_RECOVERY", "USER_INTERRUPTED", "INTERRUPTED_SIGNAL", "PROCESS_CROSSTALK",
})


def _should_persist_retry_memory(final_end_reason: str) -> bool:
    """Whether this run's trace/source is safe to feed into a later retry.

    Discovery briefs and failure-memory near-misses are intentionally prompt
    material for the next attempt. Do not persist them for runs where the agent
    retrieved a proven answer from git history or exited with an incomplete
    trace; otherwise a clean relaunch can be re-contaminated by its own retry
    hints.
    """
    return (final_end_reason or "").upper() not in _TRACE_TAINTED_RETRY_MEMORY_REASONS


# The axiom-aware admit counter `_count_llm_target_admits` is now
# imported from `lib.admits` (see top-of-file import). Same algorithm,
# pinned by tests/test_admits.py — kept aliased to the old name so
# existing callers in this file don't need to change.


def _count_gate_admits(target: Path, allow_edit: Optional[list[Path]]) -> int:
    """Count non-axiom `admit()` calls across the target plus any
    experiment_allow_edit files. Used by the COMPLETE gate so an agent
    cannot declare done while admit() placeholders remain in dep file
    bodies (relevant for proof-only mode, whose baseline seeds them)."""
    total = _count_llm_target_admits(target.read_text())
    for dep in (allow_edit or []):
        try:
            total += _count_llm_target_admits(dep.read_text())
        except OSError:
            pass
    return total


def _gate_admit_files(target: Path, allow_edit: Optional[list[Path]]) -> list[Path]:
    """Files counted by the COMPLETE admit gate, de-duplicated by resolution."""
    out: list[Path] = []
    seen: set[str] = set()
    for p in [target, *(allow_edit or [])]:
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def _active_edit_omitted_admit_files(
    target: Path,
    allow_edit: Optional[list[Path]],
    active_edit: Optional[list[Path]],
) -> list[Path]:
    """Counted files with hard admits that a strict active-edit scope freezes."""
    if not active_edit:
        return []
    active: set[str] = set()
    for p in active_edit:
        try:
            active.add(str(p.resolve()))
        except OSError:
            active.add(str(p))

    omitted: list[Path] = []
    for p in _gate_admit_files(target, allow_edit):
        if str(p) in active:
            continue
        try:
            if _count_llm_target_admits(p.read_text()) > 0:
                omitted.append(p)
        except OSError:
            continue
    return omitted


def find_cargo_root(target: Path) -> Path:
    p = target.parent if target.is_file() else target
    while p != p.parent:
        if (p / "Cargo.toml").exists():
            return p
        p = p.parent
    return target.parent


def module_path_of(target: Path, project: Path) -> str:
    rel = target.resolve().relative_to(project.resolve())
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".rs")
    if parts and parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    return "::".join(parts)


def target_needs_decompose(target: Path) -> bool:
    """Mirror of the prompt's "Decompose hard admits" trigger.

    That guidance only earns its eager cost when the target has an `admit()`
    to fill AND some function is "hard" — a body spanning >100 source lines, or
    an `ensures` clause with >=3 top-level `&&` conjuncts. The check is
    file-level and deliberately *lenient* (it doesn't insist the hard function
    be the one holding the admit): a false positive merely shows ~57 extra
    lines, whereas a false negative drops the guidance on a genuinely hard
    proof — the worse outcome. Reuses the battle-tested brace helpers from
    `lib.admits` so `ensures ({ ... })` clauses aren't misread as fn bodies.
    """
    try:
        text = target.read_text()
    except OSError:
        return False
    if "admit()" not in text:
        return False
    # Signal 1: any `ensures` clause with >=3 top-level conjuncts (>=2 `&&`).
    for m in re.finditer(r"\bensures\b", text):
        body = find_proof_fn_body_brace(text, m.start())
        clause = text[m.end(): body] if body else text[m.end():m.end() + 2000]
        dec = clause.find("decreases")          # ensures ends at decreases/body
        if dec != -1:
            clause = clause[:dec]
        if clause.count("&&") >= 2:
            return True
    # Signal 2: any function body spanning >100 source lines.
    for m in re.finditer(r"\bfn\s+\w+", text):
        body = find_proof_fn_body_brace(text, m.start())
        if body is None:
            continue
        end = find_matching_brace(text, body)
        if end is not None and text.count("\n", body, end) > 100:
            return True
    return False


def render_prompt(
    target: Path, project: Path, module: str,
    spec_snapshot: Path, catalog_cache: Path,
    results_root: Path, failure_block: str,
    vstd_root: Optional[Path] = None,
    experiment_block: str = "",
    decompose_block: str = "",
    whole_crate_assignment: bool = False,
    verus_rlimit: Optional[float] = _DEFAULT_VERUS_RLIMIT,
) -> str:
    template = PROMPT_TEMPLATE.read_text()
    vstd_flag = f" --vstd-root {vstd_root}" if vstd_root else ""
    skills_root = HERE / "skills"
    skill_doc = skills_root / "SKILL.md"
    admit_inventory_cmd = f"python3 {skills_root / 'admit_inventory.py'}"
    spec_check_cmd = f"python3 {skills_root / 'spec_check.py'}"
    base_verify_cmd = (
        f"python3 {skills_root / 'verus_check.py'} {target} --project {project}"
    )
    complete_verify_command = base_verify_cmd
    if whole_crate_assignment:
        complete_verify_command += (
            f" --whole-crate --timeout {_WHOLE_CRATE_VERUS_TIMEOUT_SEC}"
        )
    if verus_rlimit is not None and not whole_crate_assignment:
        complete_verify_command += f" --rlimit {verus_rlimit}"
    if whole_crate_assignment:
        temp_admit_rule = (
            "**No `assume(...)` and no new `admit()`.** EXPERIMENT MODE "
            "may start from deleted lemmas or stripped proof bodies, but new "
            "`admit()` checkpoints hide proof debt and contradict the "
            "whole-editable-scope COMPLETE gate. Re-create missing signatures "
            "and contracts with real proof bodies or partial proof structure; "
            "let Verus report the remaining obligations instead of masking "
            "them with `admit()`."
        )
        task_scope_intro = (
            "complete the whole editable proof-reconstruction assignment "
            "described in EXPERIMENT MODE. The target path is the harness "
            "anchor, but your job is every editable file listed there; do not "
            "stop when only the target file is green."
        )
        edit_scope_rule = (
            "**Edit only the files listed in EXPERIMENT MODE.** The editable "
            "list, not the target path, is your assignment scope. Those files "
            "may include API files, lemma files, backend/helper files, or spec "
            "modules depending on the experiment. Do not edit outside that "
            "list. Existing headers/contracts/spec definitions are frozen as "
            "described by EXPERIMENT MODE and by the spec/diff gates; new "
            "helpers must be real proofs, never `admit()`, `assume(...)`, "
            "`#[verifier::external_body]`, or new `proof fn axiom_*`."
        )
        workflow_scope_steps = (
            "0. **Plan first.** Write a short checklist of the editable files "
            "and the non-axiom admits or proof gaps you plan to attack across "
            "the whole editable list. Update it as you complete items.\n\n"
            "1. **Read the failing editable file(s).** Use the whole-crate "
            "`summary`/`messages[]` to pick the next highest-leverage "
            "obligation. Do not restrict yourself to the target file when "
            "EXPERIMENT MODE lists more editable files."
        )
        admit_scope_guidance = """To count the admits that actually matter (non-axiom only), run
   `admit_inventory.py` over the target path plus every other editable file
   listed in EXPERIMENT MODE:
   ```
   {admit_inventory_cmd} <target.rs> --siblings <editable-a.rs> <editable-b.rs> ...
   ```
   This returns JSON with `non_axiom_count`, `axiom_count`, and per-line
   entries for both. It ignores `admit()` in comments and inside
   `proof fn axiom_*` bodies. The `non_axiom_count` across the editable
   list is what must hit 0 for COMPLETE.

   A pure-shell fallback (no Python) if the skill is unavailable:
   ```
   awk 'BEGIN{a=0; c=0} /^[[:space:]]*((pub([[:space:]]*\\([^)]*\\))?|broadcast|open|closed)[[:space:]]+)*proof[[:space:]]+fn[[:space:]]+axiom_/{a=1;next} a&&/^}/{a=0;next} !a&&/admit\\(\\)/{c++} END{print FILENAME, c+0}' <editable-file> ...
   ```
   Do not pre-decide LIMIT based on raw `grep -c 'admit()'` if the
   remaining admits are all in `axiom_*` bodies.

   If even one NON-AXIOM `admit()` remains in any editable file listed in
   EXPERIMENT MODE, emit `END_REASON:LIMIT` instead."""
        session_end_checks = """Before deciding which to emit, run admit inventory across the full editable
scope:
```bash
{admit_inventory_cmd} <target.rs> --siblings <editable-a.rs> <editable-b.rs> ...
```
`non_axiom_count == 0` across the editable list -> COMPLETE eligible.
Anything > 0 -> LIMIT. Raw `grep -c 'admit()'` is not authoritative
because `axiom_*` admits are intentionally allowed to remain."""
    else:
        temp_admit_rule = (
            "**No `assume(...)`.** You may use `admit()` as a TEMPORARY "
            "checkpoint during multi-round decomposition — e.g. land 4 of 8 "
            "ensures conjuncts as real proofs, leave 4 as `admit()` for the "
            "next round. This is encouraged when working on hard proofs. But "
            "the task's FINAL round must satisfy `admits_remaining <= "
            "admits_at_start`: never end a task with more admits than it "
            "began with. Never use `assume(...)`."
        )
        task_scope_intro = (
            "replace every `admit()` in the target file with a real proof that "
            "compiles under `cargo verus`. Work only on the target file."
        )
        edit_scope_rule = (
            "**Edit only the target file -- plus new helpers in sibling "
            "`lemmas/<area>_lemmas/*.rs`.** You MAY append new `proof fn "
            "lemma_<name>(...)` declarations to any sibling "
            "`lemmas/<area>_lemmas/*.rs` file (e.g. while the target is "
            "`ristretto.rs`, you may add lemmas to "
            "`lemmas/ristretto_lemmas/elligator_lemmas.rs`). Any new helper "
            "must be a real `proof fn lemma_*` with a real proof -- you may "
            "NOT introduce a new `proof fn axiom_*` (axiom names are reserved "
            "for the pre-existing foundational axioms; their `admit()` bodies "
            "are excluded from the COMPLETE count, so a new one is a "
            "fake-green and fails the round via the axiom-integrity gate). You "
            "may NOT modify existing function signatures, bodies, "
            "requires/ensures clauses, nor remove functions in those siblings. "
            "You may NOT touch `specs/*`, `field.rs`, top-level type "
            "definitions, or any file outside `lemmas/`. The `spec_check "
            "verify` gate runs over the target AND every sibling helper in "
            "scope; signature drift in any of them fails the round.\n\n"
            "   **Sibling edits are re-verified.** After each round the "
            "harness re-runs `verus_check.py` on every sibling file you modified, "
            "plus the top-level module that consumes its area (e.g. `edwards` "
            "for any edit under `lemmas/edwards_lemmas/*`). If a sibling edit "
            "breaks that sibling's OWN verification, or breaks a module that "
            "depends on it, the round fails with `end_reason: "
            "SIBLING_VERUS_FAIL` -- keeping the target green is NOT enough. "
            "Only add lemmas to siblings whose own proofs still go through.\n\n"
            "   To see which siblings are in scope, run:\n"
            f"   `{spec_check_cmd} list-siblings <target> "
            "--project <project>`"
        )
        workflow_scope_steps = (
            "0. **Plan first.** Write a short checklist listing every "
            "`admit()` in the file as a separate item (one per fn that "
            "contains an admit). Update it as you complete items. This keeps "
            "your progress visible and prevents getting stuck on one lemma.\n\n"
            "1. **Read the target file.** Identify each `admit()`."
        )
        admit_scope_guidance = """To count the admits that actually matter (non-axiom only), prefer:
   ```
   {admit_inventory_cmd} <target.rs>
   ```
   This returns JSON with `non_axiom_count`, `axiom_count`, and per-line
   entries for both. It ignores `admit()` in comments and inside
   `proof fn axiom_*` bodies. If you've added sibling helper files, pass
   them via `--siblings <a.rs> <b.rs>` so their non-axiom admits are
   counted too.

   A pure-shell fallback (no Python) if the skill is unavailable:
   ```
   awk 'BEGIN{a=0; c=0} /^[[:space:]]*((pub([[:space:]]*\\([^)]*\\))?|broadcast|open|closed)[[:space:]]+)*proof[[:space:]]+fn[[:space:]]+axiom_/{a=1;next} a&&/^}/{a=0;next} !a&&/admit\\(\\)/{c++} END{print c+0}' <target>
   ```
   The `non_axiom_count` (or awk number) -- non-axiom admits remaining --
   is what must hit 0 for COMPLETE. Do not pre-decide LIMIT based on
   raw `grep -c 'admit()'` if the remaining admits are all in
   `axiom_*` bodies.

   If even one NON-AXIOM `admit()` remains in the target (or in any
   sibling helper you added), emit `END_REASON:LIMIT` instead."""
        session_end_checks = f"""Before deciding which to emit, run:
```bash
{admit_inventory_cmd} {target}
```
`non_axiom_count == 0` -> COMPLETE eligible. Anything > 0 -> LIMIT.
Raw `grep -c 'admit()'` is not authoritative because `axiom_*` admits
are intentionally allowed to remain."""
    admit_scope_guidance = admit_scope_guidance.replace(
        "{admit_inventory_cmd}", admit_inventory_cmd)
    session_end_checks = session_end_checks.replace(
        "{admit_inventory_cmd}", admit_inventory_cmd)
    rendered = (
        template
        .replace("{TARGET_PATH}", str(target))
        .replace("{PROJECT_ROOT}", str(project))
        .replace("{MODULE_PATH}", module)
        .replace("{SPEC_SNAPSHOT}", str(spec_snapshot))
        .replace("{CATALOG_CACHE}", str(catalog_cache))
        .replace("{RESULTS_ROOT}", str(results_root))
        .replace("{SKILLS_ROOT}", str(skills_root))
        .replace("{SKILL_DOC}", str(skill_doc))
        .replace("{VSTD_FLAG}", vstd_flag)
        .replace("{FAILURE_MEMORY_BLOCK}", failure_block or
                 "_(none — this is a fresh attempt on this function)_")
        .replace("{EXPERIMENT_MODE_BLOCK}", experiment_block)
        .replace("{DECOMPOSE_BLOCK}", decompose_block)
        .replace("{TEMP_ADMIT_RULE}", temp_admit_rule)
        .replace("{TASK_SCOPE_INTRO}", task_scope_intro)
        .replace("{EDIT_SCOPE_RULE}", edit_scope_rule)
        .replace("{WORKFLOW_SCOPE_STEPS}", workflow_scope_steps)
        .replace("{ADMIT_SCOPE_GUIDANCE}", admit_scope_guidance)
        .replace("{SESSION_END_CHECKS}", session_end_checks)
        .replace("{COMPLETE_VERIFY_COMMAND}", complete_verify_command)
    )
    # Experiment blocks are inserted after the first path-substitution pass, and
    # some mode-specific text carries the same path placeholders. Run the small
    # path pass once more so those late-inserted commands render concrete.
    return (
        rendered
        .replace("{TARGET_PATH}", str(target))
        .replace("{PROJECT_ROOT}", str(project))
        .replace("{SKILLS_ROOT}", str(skills_root))
    )


def classify_remaining_admits(target: Path,
                              extra: Optional[list[Path]] = None) -> dict:
    """Classify every remaining `admit()` as 'intentional' or 'hard', across
    `target` plus any `extra` files (experiment allow-edit deps).

    Aggregates per-file results so the `result.json` `admit_classification`
    detail covers the SAME scope as the COMPLETE gate (`_count_gate_admits`:
    target + allow-edit). Each `detail` entry carries a `file` key so a
    multi-file classification stays attributable. `extra` defaults to none, so
    the single-file callers (and the unit tests) are unchanged.
    """
    result = _classify_remaining_admits_one(target)
    for d in result["detail"]:
        d.setdefault("file", str(target))
    seen = {target.resolve()}
    for ex in (extra or []):
        try:
            if ex.resolve() in seen:
                continue
            seen.add(ex.resolve())
        except OSError:
            continue
        sub = _classify_remaining_admits_one(ex)
        for d in sub["detail"]:
            d.setdefault("file", str(ex))
        result["total"] += sub["total"]
        result["intentional"] += sub["intentional"]
        result["hard"] += sub["hard"]
        result["detail"].extend(sub["detail"])
    return result


def _classify_remaining_admits_one(target: Path) -> dict:
    """For each `admit()` in a SINGLE file `target`, classify as 'intentional'
    or 'hard'.

    Intentional signals (any one suffices):
      - File basename is `axioms.rs`
      - Enclosing fn name starts with `axiom_`
      - The docstring/comment block within ~20 lines above the enclosing
        fn contains `Axiom:` or `/// Axiom`
      - File path contains `core_assumes` or basename matches the
        documented axiom-file patterns (primality_specs, proba_specs,
        curve_equation_lemmas)

    Returns {'total': N, 'intentional': K, 'hard': N-K, 'detail': [...]}
    where `detail` is per-admit (line, enclosing_fn, classification, reason).
    """
    try:
        text = target.read_text()
    except OSError:
        return {"total": 0, "intentional": 0, "hard": 0, "detail": []}

    if "admit()" not in text:
        return {"total": 0, "intentional": 0, "hard": 0, "detail": []}

    # curve_equation_lemmas is REMOVED from this set per user direction:
    # the file contains a mix of `axiom_*` foundational propositions and
    # `lemma_*` derivable propositions. Per-fn signals (axiom_* prefix,
    # "Axiom:" docstring) suffice to classify the axiom_* ones; lemma_*
    # ones are now classified `hard` so the harness pursues them.
    is_axiom_file = (
        target.name == "axioms.rs"
        or target.stem in {"core_assumes", "primality_specs", "proba_specs"}
    )

    fn_ranges = _fn_ranges_in_file(target)
    lines = text.splitlines()  # raw lines: fn detection + "Axiom:" docstring window
    # Mask comments + string/char contents (length-preserving) so an `admit()`
    # only counts as real code — never a comment mention, and never lost to a
    # `//` inside a string literal (e.g. `let _ = "https://x"; admit();`).
    masked_lines = strip_comments_strings(text).splitlines()
    admit_lines = [i + 1 for i, code in enumerate(masked_lines)
                   if "admit()" in code]

    def find_fn(ln: int) -> Optional[tuple[str, int, int]]:
        best = None
        for name, s, e in fn_ranges:
            if s <= ln <= e and (best is None or s > best[1]):
                best = (name, s, e)
        if best is not None:
            return best
        # Fallback: the brace-walking parser may have skipped this fn
        # (e.g. unusual body, macros). Do a simple backward scan for
        # the most-recent `(pub )?(proof )?fn NAME` header above the
        # admit line. Less precise but more robust.
        header_re = re.compile(
            r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?(?:broadcast\s+)?"
            r"(?:open\s+|closed\s+)?(?:proof|spec|exec)?\s*"
            r"fn\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        for i in range(ln - 1, max(ln - 200, -1), -1):
            if i >= len(lines):
                continue
            m = header_re.match(lines[i])
            if m:
                return (m.group(1), i + 1, ln)
        return None

    detail: list[dict] = []
    for ln in admit_lines:
        enc = find_fn(ln)
        # A `lemma_*` fn with an admit() is an unfinished proof, never an
        # intentional axiom — pursue it regardless of filename/docstring.
        # Without this, a `lemma_*` obligation living in axioms.rs (or under
        # an "Axiom:" docstring) gets mis-classified intentional. Keeps this
        # consistent with _count_llm_target_admits (excludes only axiom_*).
        if enc is not None and enc[0] and enc[0].startswith("lemma_"):
            detail.append({
                "line": ln, "function": enc[0],
                "classification": "hard",
                "reason": "lemma_ fn (unfinished proof, pursued)",
            })
            continue
        reason = None
        if is_axiom_file:
            reason = f"file basename {target.name}"
        if enc is not None:
            name, s, e = enc
            if name.startswith("axiom_"):
                reason = reason or f"fn name '{name}' starts with axiom_"
            # Check ~20 lines above the fn header for "Axiom:" in comments
            window_start = max(s - 20, 0)
            window = "\n".join(lines[window_start:s])
            if "Axiom:" in window or "/// Axiom" in window:
                reason = reason or "docstring contains 'Axiom:'"
        detail.append({
            "line": ln,
            "function": enc[0] if enc else None,
            "classification": "intentional" if reason else "hard",
            "reason": reason or "not flagged",
        })

    intentional = sum(1 for d in detail if d["classification"] == "intentional")
    return {
        "total": len(detail),
        "intentional": intentional,
        "hard": len(detail) - intentional,
        "detail": detail,
    }


def _iter_assistant_blocks(raw_out: Path) -> Iterator[dict]:
    """Yield each content block of every assistant message in a round jsonl.

    Shared by the round-stream counters below: both classify assistant
    tool_use/text blocks from `claude_raw/round_N.jsonl`. Malformed lines and
    non-dict blocks are skipped; yields nothing if the file is missing or
    unreadable.
    """
    try:
        with open(raw_out) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("type") != "assistant":
                        continue
                    for c in (e.get("message", {}).get("content") or []):
                        if isinstance(c, dict):
                            yield c
                except (json.JSONDecodeError, AttributeError):
                    continue
    except OSError:
        return


_RAW_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

_MODEL_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "webSearchRequests": "web_search_requests",
    "costUSD": "cost_usd",
    "contextWindow": "context_window",
    "maxOutputTokens": "max_output_tokens",
}


def _empty_usage_totals() -> dict[str, int]:
    return {k: 0 for k in _RAW_USAGE_TOKEN_FIELDS}


def _add_token_usage(dst: dict[str, int], usage: dict) -> None:
    for k in _RAW_USAGE_TOKEN_FIELDS:
        v = usage.get(k, 0)
        if isinstance(v, (int, float)):
            dst[k] = int(dst.get(k, 0) + v)


def _max_token_usage(dst: dict[str, int], usage: dict) -> None:
    for k in _RAW_USAGE_TOKEN_FIELDS:
        v = usage.get(k, 0)
        if isinstance(v, (int, float)):
            dst[k] = max(int(dst.get(k, 0)), int(v))


def _max_model_usage(dst: dict, model_usage: object) -> None:
    if not isinstance(model_usage, dict):
        return
    for model, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        out = dst.setdefault(str(model), {})
        for src, dest in _MODEL_USAGE_FIELDS.items():
            v = usage.get(src, 0)
            if isinstance(v, (int, float)):
                out[dest] = max(out.get(dest, 0), v)


def summarize_raw_usage(raw_out: Path) -> dict:
    """Summarize diagnostic usage present in a Claude stream-json raw log.

    The final `type:"result"` usage remains the billable summary stored in
    `claude_usage`. This helper is intentionally diagnostic: it also works when
    there is no final result event (SIGKILL/user interrupt) and captures
    subagent/modelUsage/task_progress totals that the final result can obscure.
    Assistant messages can appear in multiple chunks with the same message id,
    so both event-level sums and message-id-deduped sums are recorded.
    """
    summary = {
        "jsonl_lines": 0,
        "parse_errors": 0,
        "assistant_usage_events": 0,
        "assistant_usage_unique_messages": 0,
        "assistant_usage_event_sums": _empty_usage_totals(),
        "assistant_usage_unique_message_sums": _empty_usage_totals(),
        "assistant_usage_event_max": _empty_usage_totals(),
        "result_events": 0,
        "result_usage_event_sums": _empty_usage_totals(),
        "result_usage_event_max": _empty_usage_totals(),
        "result_total_cost_usd_max": 0.0,
        "model_usage_events": 0,
        "model_usage_by_model_max": {},
        "task_progress_events": 0,
        "task_progress_latest_total_tokens": 0,
        "task_progress_latest_tool_uses": 0,
        "task_progress_latest_duration_ms": 0,
    }
    unique_assistant_usage: dict[str, dict] = {}
    task_progress_latest: dict[str, dict] = {}

    try:
        with open(raw_out) as f:
            for line_no, line in enumerate(f, 1):
                summary["jsonl_lines"] = line_no
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    summary["parse_errors"] += 1
                    continue
                if not isinstance(e, dict):
                    continue

                if e.get("type") == "assistant":
                    message = e.get("message") or {}
                    usage = message.get("usage") or {}
                    if isinstance(usage, dict) and usage:
                        summary["assistant_usage_events"] += 1
                        _add_token_usage(summary["assistant_usage_event_sums"], usage)
                        _max_token_usage(summary["assistant_usage_event_max"], usage)
                        mid = message.get("id") or f"line:{line_no}"
                        prev = unique_assistant_usage.setdefault(str(mid), {})
                        _max_token_usage(prev, usage)

                if e.get("type") == "result":
                    summary["result_events"] += 1
                    usage = e.get("usage") or {}
                    if isinstance(usage, dict) and usage:
                        _add_token_usage(summary["result_usage_event_sums"], usage)
                        _max_token_usage(summary["result_usage_event_max"], usage)
                    cost = e.get("total_cost_usd", 0.0)
                    if isinstance(cost, (int, float)):
                        summary["result_total_cost_usd_max"] = max(
                            summary["result_total_cost_usd_max"], float(cost))
                    model_usage = e.get("modelUsage")
                    if isinstance(model_usage, dict) and model_usage:
                        summary["model_usage_events"] += 1
                        _max_model_usage(
                            summary["model_usage_by_model_max"], model_usage)

                if e.get("subtype") == "task_progress":
                    usage = e.get("usage") or {}
                    if isinstance(usage, dict) and usage:
                        summary["task_progress_events"] += 1
                        task_id = str(e.get("task_id") or f"line:{line_no}")
                        task_progress_latest[task_id] = usage
    except OSError:
        return summary

    summary["assistant_usage_unique_messages"] = len(unique_assistant_usage)
    for usage in unique_assistant_usage.values():
        _add_token_usage(summary["assistant_usage_unique_message_sums"], usage)
    for usage in task_progress_latest.values():
        for src, dest in (
            ("total_tokens", "task_progress_latest_total_tokens"),
            ("tool_uses", "task_progress_latest_tool_uses"),
            ("duration_ms", "task_progress_latest_duration_ms"),
        ):
            v = usage.get(src, 0)
            if isinstance(v, (int, float)):
                summary[dest] += int(v)
    return summary


def count_agent_actions(raw_out: Path) -> int:
    """Count productive agent actions in the round's raw event stream.

    Reads `claude_raw/round_N.jsonl` and counts assistant tool_uses and text
    blocks. Used as a productivity signal that survives SIGKILL (when the
    final `result` event — and thus `claude_usage` — is missing).

    Returns 0 if file missing or unreadable.
    """
    return sum(1 for c in _iter_assistant_blocks(raw_out)
               if c.get("type") in ("tool_use", "text"))


def count_agent_delegations(raw_out: Path) -> int:
    """Count legacy `Agent` (subagent) tool-uses in the round's raw event stream.

    Reads `claude_raw/round_N.jsonl` and counts assistant tool_use blocks
    whose tool name is `Agent` (the literal name the subagent-spawning tool
    emits in older headless `claude -p` streams; the older `Task` wording is
    tolerated). Current proof-agent runs disable this tool, so new counts should
    stay at zero; the metric remains useful when auditing older raw traces.
    Returns 0 if file missing.
    """
    return sum(1 for c in _iter_assistant_blocks(raw_out)
               if c.get("type") == "tool_use"
               and c.get("name") in ("Agent", "Task"))


_BROAD_PROCESS_CONTROL_RE = re.compile(
    r"\b(?:pkill|killall)\b|\bpgrep\s+-f\b"
)
_SHELL_TIMEOUT_RE = re.compile(
    r"(?:^|[;&|]\s*)timeout\s+(?:-\S+\s+)*\d"
)
_VERIFIER_PROCESS_RE = re.compile(
    r"\b(?:verus_check\.py|cargo\s+verus|cargo-verus|rust_verify|z3)\b"
)
_VERUS_CHECK_HELP_RE = re.compile(
    r"\bverus_check\.py\b[\s\S]*?(?:^|\s)(?:-h|--help)(?:\s|$)"
)
_SHARED_TMP_OUTPUT_RE = re.compile(
    r"(?:^|[\s>|])(?:/tmp/)(?!dalek-|claude-|codex-)[A-Za-z0-9_.-]+"
    r"\.(?:json|out|log)\b"
)


# Hook-specific evidence that a Bash command was BLOCKED by a PreToolUse hook
# (exit 2) and so NEVER executed. The runtime — not our hook — frames a
# PreToolUse hook error as an `is_error` tool_result whose content is prefixed
# `PreToolUse:Bash hook error:` (verified against the headless smoke stream:
# `PreToolUse:Bash hook error: [<cmd>]: <stderr>`). We deliberately key on this
# runtime wrapper, NOT our own stderr phrase ("BLOCKED by verifier policy"): an
# executed/bypassed command could `echo` our phrase into its own tool_result and
# hide from the crosstalk audit, but it cannot make the runtime prefix a normal
# command result with the PreToolUse-hook-error wrapper.
_HOOK_BLOCK_RE = re.compile(r"pretooluse:\s*bash\s+hook\s+error", re.I)


def _tool_result_text(block: dict) -> str:
    """Flatten a tool_result's `content` (str or list-of-blocks) to plain text."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or c.get("content") or ""))
            elif isinstance(c, str):
                parts.append(c)
        return " ".join(parts)
    return ""


def _hook_blocked_tool_use_ids(raw_out: Path) -> set[str]:
    """tool_use ids whose Bash command the PreToolUse hook BLOCKED (never ran).

    The PreToolUse verifier-policy hook hard-blocks the forbidden verifier shapes
    AT the tool call (exit 2) and feeds the model a corrective message — the
    command does not execute. But the attempted tool_use still appears in the raw
    stream, so detect_process_crosstalk must skip those ids: a hook-blocked,
    never-executed command is not a terminal crosstalk failure (the hook already
    corrected it in-round). Commands that actually executed — or bypassed the hook
    — leave no block marker and stay subject to the terminal audit.
    """
    blocked: set[str] = set()
    try:
        with open(raw_out) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict) or e.get("type") != "user":
                    continue
                for c in (e.get("message", {}).get("content") or []):
                    if not isinstance(c, dict) or c.get("type") != "tool_result":
                        continue
                    tid = c.get("tool_use_id")
                    # Genuine PreToolUse hook blocks come back as an error
                    # tool_result with the runtime wrapper; require both so an
                    # executed command's stdout cannot forge a suppression.
                    if (tid and c.get("is_error")
                            and _HOOK_BLOCK_RE.search(_tool_result_text(c))):
                        blocked.add(tid)
    except OSError:
        return blocked
    return blocked


def _is_verus_check_help_only(cmd: str) -> bool:
    """Return true when verifier-looking segments only ask for verus_check help."""
    segments = [s for s in re.split(r"[;&]", cmd) if _VERIFIER_PROCESS_RE.search(s)]
    return bool(segments) and all(
        _VERUS_CHECK_HELP_RE.search(segment)
        and not re.search(r"\bcargo\s+verus|cargo-verus|rust_verify|z3\b", segment)
        for segment in segments
    )


def detect_process_crosstalk(raw_out: Path) -> list[str]:
    """Return Bash commands that risk shared process/tmp crosstalk.

    The proof agent runs in a subprocess group, but its Bash tool still sees the
    user's process namespace and global `/tmp`. Broad `pkill`/`killall`/`pgrep
    -f`, shell timeout wrappers, background verifier jobs, and verifier JSON
    redirected to fixed `/tmp/foo.json` paths can interfere with concurrent runs
    or make the agent read stale output. Return short, deduped descriptions for
    round telemetry.

    Bash tool_use attempts that the PreToolUse verifier-policy hook BLOCKED are
    excluded: those commands never executed (the hook corrected them in-round),
    so they must not be reclassified as a terminal PROCESS_CROSSTALK after the
    round. Only commands that actually ran (or bypassed the hook) are reported.
    """
    blocked_ids = _hook_blocked_tool_use_ids(raw_out)
    hits: list[str] = []
    seen: set[str] = set()
    for c in _iter_assistant_blocks(raw_out):
        if c.get("type") != "tool_use" or c.get("name") != "Bash":
            continue
        if c.get("id") in blocked_ids:
            continue  # hook blocked this command in-round; it never executed
        inp = c.get("input") or {}
        cmd = (inp.get("command") or "")
        if not isinstance(cmd, str) or not cmd:
            continue
        verifier_like = bool(_VERIFIER_PROCESS_RE.search(cmd))
        if verifier_like and _is_verus_check_help_only(cmd):
            verifier_like = False
        reasons: list[str] = []
        if _BROAD_PROCESS_CONTROL_RE.search(cmd):
            reasons.append("broad process control")
        if _SHELL_TIMEOUT_RE.search(cmd) and verifier_like:
            reasons.append("shell timeout verifier wrapper")
        if inp.get("run_in_background") and verifier_like:
            reasons.append("background verifier")
        if verifier_like and _SHARED_TMP_OUTPUT_RE.search(cmd):
            reasons.append("shared /tmp verifier output")
        if not reasons:
            continue
        item = f"{', '.join(reasons)}: {cmd.strip()[:200]}"
        if item not in seen:
            seen.add(item)
            hits.append(item)
    return hits


# Diff sub-flags that show only metadata (no file content), so they are NOT
# answer-recovery — the agent may legitimately inspect them.
_DIFF_METADATA_ONLY = ("--stat", "--numstat", "--shortstat", "--name-only",
                       "--name-status", "--dirstat", "--summary")


_PATCH_LOG_RE = re.compile(r"(^|\s)(-p|-u|--patch|--patch-with-stat|-G\S|-S\S)")


def _is_frozen_diff_noise_path(path: str) -> bool:
    """True for generated verifier sidecars that are not proof source edits."""
    return path == ".verilib" or path.startswith(".verilib/")


def _frozen_paths_from_diff_name_status_z(out: str,
                                          allow_edit_rel: set[str]) -> list[str]:
    """Return changed paths outside allow_edit from `git diff --name-status -z`.

    Rename/copy records carry two paths; include both sides so recovery can
    restore the deleted old path and remove the added new path. Keep this pure
    so the frozen-file guard's Git parsing is unit-testable without a repo.
    """
    parts = out.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    frozen: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        npaths = 2 if status[0] in ("R", "C") else 1
        for p in parts[i:i + npaths]:
            if (p and not _is_frozen_diff_noise_path(p)
                    and p not in allow_edit_rel and p not in seen):
                seen.add(p)
                frozen.append(p)
        i += npaths
    return sorted(frozen)


def _frozen_paths_changed_from_git(
    project: Path,
    allow_edit_rel: set[str],
    env: Optional[dict[str, str]] = None,
) -> tuple[list[str], Optional[str]]:
    """Audit frozen-file edits against git HEAD.

    Return `(paths, None)` when the audit succeeds. Return `([], error)` when
    git cannot answer the question at all; callers must treat that as
    fail-closed, not as "no frozen files changed".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "diff", "--name-status", "-z", "HEAD"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return [], f"git diff failed: {e!r}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            detail = detail[-1000:]
        else:
            detail = f"rc={proc.returncode}"
        return [], f"git diff rc={proc.returncode}: {detail}"
    return _frozen_paths_from_diff_name_status_z(proc.stdout, allow_edit_rel), None


# Tokens that, inside a git command, name a NON-HEAD revision. On a SEALED
# worktree (peel re-roots HEAD on the stripped 'history sealed' orphan — see
# peel._seal_peeled_history) the ONLY way to reach the dangling proven objects is
# one of these; everything else resolves to the stripped HEAD / working tree /
# index. So the sealed allowlist = "no non-HEAD revision token present".
_NON_HEAD_REV_RE = re.compile(
    r"\b[0-9a-f]{7,40}\b"                       # raw commit sha
    r"|@\{"                                    # reflog expr (HEAD@{1}, @{0})
    r"|\bHEAD[\^~]"                            # HEAD ancestry (HEAD^, HEAD~N) → a parent
    r"|(?:^|\s)(?:main|master)(?=$|\s|:|\^|~)"  # leftover branch refs (not path/main.rs)
    r"|origin/|refs/"                          # remote / fully-qualified refs
    r"|--(?:all|reflog|branches|tags)\b"       # all-refs traversal
)


_GIT_ALT_REPO_GLOBAL_OPTS = ("-C", "--git-dir", "--work-tree", "--namespace")
_GIT_ARG_GLOBAL_OPTS = {
    "-c", "--config-env", "--exec-path",
    *_GIT_ALT_REPO_GLOBAL_OPTS,
}
_GIT_NO_ARG_GLOBAL_OPTS = {
    "--bare", "--glob-pathspecs", "--help", "--html-path",
    "--icase-pathspecs", "--literal-pathspecs", "--man-path",
    "--no-pager", "--no-replace-objects", "--noglob-pathspecs",
    "--paginate", "--version",
}


def _parse_git_subcommand(tail: str) -> tuple[bool, str, str]:
    """Return `(alternate_repo, subcommand, rest)` for text after `git`.

    Git accepts global options before the subcommand. Some take an argument
    (`-c key=value`, `--config-env name=env`), so regex parsing can mistake that
    argument for the subcommand. Alternate-repo options are recovery in every
    mode because they point the agent at a different object store/worktree.
    """
    try:
        toks = shlex.split(tail)
    except ValueError:
        toks = tail.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "--":
            i += 1
            break
        if tok.startswith("-C") and tok != "-c":
            return True, "", ""
        if tok in _GIT_ALT_REPO_GLOBAL_OPTS or any(
            tok.startswith(f"{opt}=") for opt in _GIT_ALT_REPO_GLOBAL_OPTS
            if opt.startswith("--")
        ):
            return True, "", ""
        if tok in _GIT_ARG_GLOBAL_OPTS:
            i += 2
            continue
        if any(tok.startswith(f"{opt}=") for opt in _GIT_ARG_GLOBAL_OPTS
               if opt.startswith("--")):
            i += 1
            continue
        if tok in _GIT_NO_ARG_GLOBAL_OPTS:
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        break
    if i >= len(toks):
        return False, "", ""
    return False, toks[i], " ".join(toks[i + 1:])


def _git_segment_recovers_source(seg: str, sealed: bool = False) -> bool:
    """True if a single shell segment is a git command that can surface the
    ORIGINAL (pre-strip) content of a tracked file from history/refs.

    Unsealed (the committed HEAD/main + reflog hold the full original):
    `git show`, `restore`, `checkout <ref> -- `, `log -p`, `diff` (vs HEAD — the
    `-` lines ARE the answer), `cat-file`, `stash show -p`, `worktree add`,
    `reflog` all recover it; metadata forms (`status`, `--stat`, `log --oneline`)
    do not.

    Sealed (HEAD is the stripped orphan): allow ONLY exact-HEAD / working-tree /
    index reads — `status`, metadata or working-tree/HEAD `diff`, `show HEAD:<f>`,
    `restore`/`checkout` from HEAD. Block anything naming a non-HEAD revision
    (`_NON_HEAD_REV_RE`: sha, reflog, HEAD^/HEAD~, main/master, origin/*, refs/*,
    --all/…), plus raw-object reads (`cat-file`, `archive`, `worktree add`) and
    patch logs. This keeps the seal's no-leak guarantee while not nuking a round
    for a diagnostic `git diff`/`git checkout HEAD -- f` (corefloor_006_resume3
    r2: 47→8 admits were discarded for exactly such commands)."""
    m = re.search(r"\bgit\b\s+(.*)", seg, re.S)
    if not m:
        return False
    tail = m.group(1)
    # Alternate-repo / alternate-worktree redirection points git at a DIFFERENT
    # checkout/object store. During proof work the only purpose is to read the
    # proven source from elsewhere, so the seal on THIS worktree is irrelevant.
    alt_repo, sub, rest = _parse_git_subcommand(tail)
    if alt_repo:
        return True
    if not sub:
        return False
    # `git reflog` and `git fsck` surface dangling proven/WIP object ids in BOTH
    # modes — pure recovery enablement (the gcp10 restored-.git trace exposed
    # unreachable WIP commits via fsck even with no refs or working-tree .rs).
    if sub in ("reflog", "fsck"):
        return True
    if sealed:
        if _NON_HEAD_REV_RE.search(rest):
            return True
        if sub in ("cat-file", "archive"):
            return True
        if sub == "worktree":
            return "add" in rest
        if sub == "log":
            return bool(_PATCH_LOG_RE.search(rest))
        if sub == "stash":
            return "show" in rest and ("-p" in rest or "--patch" in rest)
        return False   # status / HEAD-or-worktree diff / show HEAD: / restore / checkout HEAD
    if sub in ("show", "cat-file", "restore"):
        return True
    if sub == "checkout":
        # branch switch is fine; restoring a file from a ref/pathspec is recovery
        return "--" in rest or bool(
            re.search(r"\b(HEAD|main|master|origin/\S+|[0-9a-f]{7,40})\b", rest))
    if sub == "log":
        return bool(_PATCH_LOG_RE.search(rest))
    if sub == "diff":
        return not any(f in rest for f in _DIFF_METADATA_ONLY)
    if sub == "stash":
        return "show" in rest and ("-p" in rest or "--patch" in rest)
    if sub == "worktree":
        return "add" in rest  # could check out the pristine tree elsewhere
    return False


def _is_sealed_worktree(project: Path) -> bool:
    """True only for a peel-sealed worktree: HEAD is the stripped 'history
    sealed' ORPHAN commit (peel._seal_peeled_history). Require BOTH signals —
    (1) HEAD has no parent, and (2) its subject carries the seal sentinel — so a
    coincidental orphan HEAD holding real content (e.g. a fresh `git init` of the
    proven tree) is NOT mistaken for sealed and granted the relaxed git policy.
    Regular admitted checkouts have deep history and fail (1). The sentinel
    string is peel.py's seal message; keep them in sync."""
    try:
        no_parent = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--verify", "--quiet",
             "HEAD^"], capture_output=True, text=True).returncode != 0
        subject = subprocess.run(
            ["git", "-C", str(project), "log", "-1", "--format=%s", "HEAD"],
            capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return no_parent and "history sealed" in subject


def _git_unreachable_object_lines(fsck_output: str,
                                  max_entries: int = 20) -> list[str]:
    """Suspicious unreachable git objects surfaced by `git fsck`.

    In a sealed proof run, dangling commits/blobs/trees can be enough to recover
    stripped source by id, as gcp10 demonstrated. Keep this parser pure so the
    policy can be unit-tested without relying on local git behavior.
    """
    hits: list[str] = []
    for line in fsck_output.splitlines():
        stripped = line.strip()
        if re.search(r"\bunreachable\s+(commit|tree|blob|tag)\b", stripped):
            hits.append(stripped)
            if len(hits) >= max_entries:
                break
    return hits


def _sealed_git_object_leaks(project: Path) -> tuple[list[str], Optional[str]]:
    """Return unreachable git objects in a sealed worktree, or an audit error."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "fsck", "--no-reflogs",
             "--unreachable", "--no-progress"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return [], f"git fsck failed: {e!r}"
    combined = "\n".join(x for x in (proc.stdout, proc.stderr) if x)
    leaks = _git_unreachable_object_lines(combined)
    if leaks:
        return leaks, None
    if proc.returncode != 0:
        detail = combined.strip()[-1000:] if combined.strip() else f"rc={proc.returncode}"
        return [], f"git fsck rc={proc.returncode}: {detail}"
    return [], None


def detect_git_recovery(raw_out: Path, sealed: bool = False) -> list[str]:
    """Return the agent Bash commands in this round that recover original
    source from version control (the answer leaks through the worktree's git
    history). Each is a fake-green vector: the agent can `git show HEAD:<file>`
    the deleted proof/lemmas and copy them back, so a green verus is not
    evidence of reconstruction. Returns the offending segments (deduped).

    `sealed`: the worktree is re-rooted on a stripped orphan commit, so only
    explicit non-HEAD references leak (see _git_segment_recovers_source)."""
    hits: list[str] = []
    seen: set[str] = set()
    for c in _iter_assistant_blocks(raw_out):
        if c.get("type") != "tool_use" or c.get("name") != "Bash":
            continue
        cmd = (c.get("input") or {}).get("command", "") or ""
        for seg in re.split(r"&&|\|\||[;|\n]", cmd):
            if _git_segment_recovers_source(seg, sealed=sealed):
                s = seg.strip()[:200]
                if s not in seen:
                    seen.add(s)
                    hits.append(s)
    return hits


def _snapshot_name(f: Path, files: list[Path]) -> str:
    """Stable flat filename for `f` inside a per-round snapshot dir.

    Keep the common case readable (`foo.rs`). If multiple snapshotted files
    share a basename (`mod.rs`, `axioms.rs`, etc.), include the parent plus a
    short path hash so rollback never maps two source files to one snapshot.
    """
    if sum(1 for p in files if p.name == f.name) <= 1:
        return f.name
    digest = hashlib.sha1(str(f.resolve()).encode()).hexdigest()[:8]
    return f"{f.parent.name}__{digest}__{f.name}"


def snapshot_files(files: list[Path], dest_dir: Path) -> None:
    """Copy each file into dest_dir under stable, collision-free flat names.

    Used to record per-round state of target + sibling helpers / allow-edit deps
    so we can diff and surface what the previous round attempted.
    """
    import shutil
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.exists():
            continue
        out = dest_dir / _snapshot_name(f, files)
        shutil.copy2(f, out)


def snapshot_partial_round_state(
    files: list[Path], snapshots_root: Path, round_number: int
) -> dict:
    """Best-effort snapshot for a tainted no-result/interrupted round."""
    dest_dir = snapshots_root / f"round_{round_number}_partial"
    manifest = {
        "round_number": round_number,
        "snapshot_dir": str(dest_dir),
        "tainted": True,
        "files": [],
    }
    try:
        snapshot_files(files, dest_dir)
    except Exception as e:
        manifest["snapshot_error"] = repr(e)

    for f in files:
        entry = {
            "source": str(f),
            "snapshot": _snapshot_name(f, files),
            "exists": f.exists(),
        }
        snap = dest_dir / entry["snapshot"]
        if snap.exists():
            try:
                entry["sha256"] = hashlib.sha256(snap.read_bytes()).hexdigest()
                entry["bytes"] = snap.stat().st_size
            except OSError as e:
                entry["error"] = repr(e)
        manifest["files"].append(entry)

    try:
        write_json(dest_dir / "manifest.json", manifest)
    except Exception as e:
        manifest["manifest_error"] = repr(e)
    return manifest


def _project_relative_path(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)\n"


def _git_diff_name_status(
    project: Path, files: list[Path], max_entries: int = 80
) -> tuple[list[dict], Optional[str], int]:
    rels = [_project_relative_path(project, f) for f in files]
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "diff", "--name-status", "HEAD", "--", *rels],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return [], repr(e), 0
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout)[-1000:], 0

    entries: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            entries.append({"status": parts[0], "paths": parts[1:]})
    total = len(entries)
    return entries[:max_entries], None, max(0, total - max_entries)


def _git_diff_excerpt(project: Path, file: Path, max_chars: int = 12000) -> str:
    rel = _project_relative_path(project, file)
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "diff", "--no-ext-diff",
             "--unified=3", "HEAD", "--", rel],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return f"<git diff failed: {e!r}>"
    if proc.returncode != 0:
        return f"<git diff failed: {(proc.stderr or proc.stdout)[-1000:]}>"
    return _truncate_text(proc.stdout, max_chars)


def snapshot_post_agent_round_state(
    project: Path,
    files: list[Path],
    snapshots_root: Path,
    tdir: Path,
    round_number: int,
    target: Path,
) -> dict:
    """Capture worktree state immediately after Claude exits, before gates.

    This is diagnostic-only. It gives monitors an auditable source snapshot
    during long post-round Verus checks, before round_N.json and the normal
    round-history snapshot exist.
    """
    dest_dir = snapshots_root / f"round_{round_number}_agent"
    diff_entries, diff_error, diff_truncated = _git_diff_name_status(project, files)
    manifest = {
        "round_number": round_number,
        "phase": "post_claude_pre_verus",
        "snapshot_dir": str(dest_dir),
        "diff_name_status": diff_entries,
        "diff_name_status_truncated": diff_truncated,
        "target_diff_excerpt": "",
        "files": [],
    }
    if diff_error:
        manifest["diff_error"] = diff_error

    try:
        snapshot_files(files, dest_dir)
    except Exception as e:
        manifest["snapshot_error"] = repr(e)

    for f in files:
        entry = {
            "source": str(f),
            "snapshot": _snapshot_name(f, files),
            "exists": f.exists(),
        }
        snap = dest_dir / entry["snapshot"]
        if snap.exists():
            try:
                entry["sha256"] = hashlib.sha256(snap.read_bytes()).hexdigest()
                entry["bytes"] = snap.stat().st_size
            except OSError as e:
                entry["error"] = repr(e)
        manifest["files"].append(entry)

    manifest["target_diff_excerpt"] = _git_diff_excerpt(project, target)
    state_path = tdir / f"round_{round_number}_agent_state.json"
    manifest["manifest_path"] = str(state_path)
    try:
        write_json(state_path, manifest)
    except Exception as e:
        manifest["manifest_error"] = repr(e)
    return manifest


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# Map a `lemmas/<area>_lemmas/...` sibling to the top-level module(s) that
# depend on that area. Used by the sibling-verify gate: when the agent edits
# a sibling helper, `--verify-module <TARGET>` won't re-check the top-level
# module that consumes it, so we re-verify these explicitly. Keep adjacent to
# LAYER_SETS in run_layer.py conceptually — add an entry if a new area lands.
_AREA_TOP_LEVEL: dict[str, list[str]] = {
    "field": ["field", "backend::serial::u64::field"],
    "scalar": ["scalar", "backend::serial::u64::scalar"],
    "edwards": ["edwards"],
    "montgomery": ["montgomery"],
    "ristretto": ["ristretto"],
}


def _area_top_level_modules(sib_path: Path) -> list[str]:
    """Top-level module(s) depending on the area of a `lemmas/<area>_lemmas`
    sibling. Returns [] for areas with no top-level consumer (e.g.
    common_lemmas) or paths outside a lemmas/ tree."""
    parts = sib_path.parts
    if "lemmas" not in parts:
        return []
    i = parts.index("lemmas")
    if i + 1 >= len(parts):
        return []
    comp = parts[i + 1]  # e.g. 'field_lemmas', 'scalar_lemmas.rs', 'edwards_lemmas'
    for area, mods in _AREA_TOP_LEVEL.items():
        if comp.startswith(area):
            return mods
    return []


_FN_HEADER_RE = re.compile(
    r"^[ \t]*(?:#\[[^\]]+\]\s*)*"
    r"(?:pub(?:\s*\([^)]+\))?\s+)?"
    r"(?:broadcast\s+)?"
    r"(?:open\s+|closed\s+)?"
    r"(?:proof|spec|exec)?\s*"
    r"fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _fn_ranges_in_file(file_path: Path) -> list[tuple[str, int, int]]:
    """Return [(fn_name, start_line, end_line), ...] for a Rust file.

    Walks brace depth from each `fn` header to find the closing `}`.
    Used to filter diff hunks by enclosing function in the round-history
    block: hunks inside a fn that no longer contains `admit()` get
    dropped, because their verified state is already encoded in the file
    the agent reads fresh each round.

    Best-effort: skips fns the parser can't bracket-match cleanly.
    Handles `//` line comments, `/* */` block comments, and `"..."`
    string literals while walking. Char literals and lifetimes are not
    handled — Rust's `'a` lifetimes vs `'a'` char literals are ambiguous
    without a real lexer, so we ignore single-quote entirely.
    """
    try:
        text = file_path.read_text()
    except OSError:
        return []
    ranges: list[tuple[str, int, int]] = []
    for m in _FN_HEADER_RE.finditer(text):
        name = m.group("name")
        start_pos = m.start()
        start_line = text.count("\n", 0, start_pos) + 1
        # Find the first `{` after the header (skip requires/ensures/etc).
        # Then walk balanced braces to the matching `}`.
        #
        # `sig_depth` tracks `(...)`/`[...]` nesting so a `;` *inside* the
        # signature is not mistaken for a forward-declaration terminator. Rust
        # array types (`[u64; 5]`, `[u8; 32]`) embed a `;` — without this the
        # parser bailed on every fn with an array-typed param/return, which is
        # most of the field lemmas (e.g. all of mul_lemmas.rs).
        i = m.end()
        brace_start = -1
        sig_depth = 0
        in_str = False
        lc = False  # line comment
        bc = False  # block comment
        while i < len(text):
            c = text[i]
            if lc:
                if c == "\n":
                    lc = False
            elif bc:
                if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                    bc = False
                    i += 1
            elif in_str:
                if c == "\\" and i + 1 < len(text):
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                lc = True
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                bc = True
                i += 1
            elif c == '"':
                in_str = True
            elif c in "([":
                sig_depth += 1
            elif c in ")]":
                sig_depth -= 1
            elif c == "{" and sig_depth == 0:
                brace_start = i
                break
            elif c == ";" and sig_depth == 0:
                # Forward declaration (no body) — skip.
                brace_start = -2
                break
            i += 1
        if brace_start < 0:
            continue
        # Walk balanced braces from brace_start.
        depth = 1
        i = brace_start + 1
        in_str = False
        lc = False
        bc = False
        while i < len(text) and depth > 0:
            c = text[i]
            if lc:
                if c == "\n":
                    lc = False
            elif bc:
                if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                    bc = False
                    i += 1
            elif in_str:
                if c == "\\" and i + 1 < len(text):
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                lc = True
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                bc = True
                i += 1
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        end_line = text.count("\n", 0, i) + 1
        ranges.append((name, start_line, end_line))
    return ranges


def _in_progress_fns(file_path: Path) -> set[str]:
    """Return set of fn names in `file_path` whose body still contains `admit()`."""
    try:
        text = file_path.read_text()
    except OSError:
        return set()
    if "admit()" not in text:
        return set()
    ranges = _fn_ranges_in_file(file_path)
    if not ranges:
        return set()
    # For each admit() occurrence, find enclosing fn by line number.
    lines = text.splitlines()
    admit_lines = [i + 1 for i, ln in enumerate(lines) if "admit()" in ln]
    out: set[str] = set()
    for ln in admit_lines:
        # Innermost fn containing ln (most-recently-started before ln).
        best: Optional[tuple[str, int, int]] = None
        for name, s, e in ranges:
            if s <= ln <= e:
                if best is None or s > best[1]:
                    best = (name, s, e)
        if best is not None:
            out.add(best[0])
    return out


_HUNK_HEADER_RE = re.compile(
    r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@",
    re.MULTILINE,
)


def _split_diff_into_hunks(diff_text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Split a unified-diff text into (header, [(new_start, new_end, hunk_text), ...]).

    The header is the leading `---`/`+++` lines (kept verbatim). Each hunk
    starts at a `@@ ... @@` line and runs until the next `@@` or EOF.
    new_start / new_end are 1-indexed line numbers in the NEW (post-edit)
    file, derived from the `+L,n` portion of the hunk header.
    """
    lines = diff_text.splitlines(keepends=True)
    # Header = lines before the first @@ header.
    first_hunk = None
    for i, ln in enumerate(lines):
        if ln.startswith("@@"):
            first_hunk = i
            break
    if first_hunk is None:
        # No hunks
        return diff_text, []
    header = "".join(lines[:first_hunk])
    hunks: list[tuple[int, int, str]] = []
    cur_start: Optional[int] = None
    cur_end: Optional[int] = None
    cur_buf: list[str] = []
    for ln in lines[first_hunk:]:
        m = _HUNK_HEADER_RE.match(ln)
        if m:
            if cur_buf:
                hunks.append((cur_start, cur_end, "".join(cur_buf)))
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) else 1
            cur_start = new_start
            cur_end = new_start + max(new_count - 1, 0)
            cur_buf = [ln]
        else:
            cur_buf.append(ln)
    if cur_buf:
        hunks.append((cur_start, cur_end, "".join(cur_buf)))
    return header, hunks


def _enclosing_fn(ranges: list[tuple[str, int, int]], line: int) -> Optional[str]:
    """Return innermost fn name containing `line`, or None."""
    best: Optional[tuple[str, int, int]] = None
    for name, s, e in ranges:
        if s <= line <= e:
            if best is None or s > best[1]:
                best = (name, s, e)
    return best[0] if best else None


def _filter_diff_to_in_progress(
    diff_text: str,
    fn_ranges: list[tuple[str, int, int]],
    in_progress_fns: set[str],
) -> str:
    """Drop diff hunks whose enclosing fn is fully verified. Keep hunks
    whose enclosing fn is admit-bearing or undeterminable. Returns the
    filtered diff text (empty string if nothing survives)."""
    header, hunks = _split_diff_into_hunks(diff_text)
    if not hunks:
        return diff_text
    kept_blobs: list[str] = []
    for new_start, new_end, blob in hunks:
        # Probe a few lines spanning the hunk to find an enclosing fn.
        probes = [new_start, (new_start + new_end) // 2, new_end]
        encl = None
        for p in probes:
            encl = _enclosing_fn(fn_ranges, p)
            if encl is not None:
                break
        if encl is None:
            # Outside any fn (e.g. impl-level, module-level) — keep.
            kept_blobs.append(blob)
            continue
        if encl in in_progress_fns:
            kept_blobs.append(blob)
        # else: drop — fn no longer has admit(), verified work
    if not kept_blobs:
        return ""
    return header + "".join(kept_blobs)


def _loc_in_target(msg_file: str, target: Path) -> bool:
    """True if a Verus diagnostic location (`curve25519-dalek/src/.../x.rs`,
    relative to the cargo workspace root) points at `target`. Verus prints
    workspace-relative paths; `target` is whatever run.py was handed. Match by
    path suffix in either direction so a bare basename collision (two
    `mul_lemmas.rs` in different dirs) does not produce a false hit."""
    if not msg_file:
        return False
    mf = msg_file.replace("\\", "/")
    try:
        tp = str(target.resolve()).replace("\\", "/")
    except OSError:
        tp = str(target).replace("\\", "/")
    return tp.endswith(mf) or mf.endswith(tp)


def _extract_near_miss(target: Path, failed_decls: list[str],
                       error_locs: Optional[list[tuple[str, int]]] = None,
                       max_decls: int = 3, max_lines_per: int = 70,
                       ) -> tuple[list[str], str]:
    """Feature 1 — pull the source of the declarations Verus rejected, from
    the target file as it stands at the end of a failed run (the agent's
    near-miss attempt). Stored into failure memory so the next attempt starts
    from the code that almost worked rather than from raw stderr alone.

    Returns `(resolved_fn_names, source)`. Resolution is best-effort against
    `_fn_ranges_in_file`:
      1. exact / last-`::`-segment name match on `failed_decls`;
      2. line-number fallback — map each Verus error location in `target` to
         its enclosing fn.
    The fallback matters because on current Verus the parsed
    `failed_declarations` are unusable for this (a precondition/postcondition
    failure prints a `file:line` location but no fn name in backticks, so the
    name regex captures the crate name or nothing). The reliable signal is the
    error line, which `_fn_ranges_in_file` brackets back to a fn.

    Returns `([], "")` when nothing maps to a parseable fn — never wrong code."""
    try:
        lines = target.read_text().splitlines()
    except OSError:
        return [], ""
    fn_ranges = _fn_ranges_in_file(target)               # [(name, s, e), ...]
    ranges = {name: (s, e) for name, s, e in fn_ranges}
    ordered: list[str] = []                              # resolved names, in order
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)

    # 1. name match (handles Verus builds / cases that do emit a fn name)
    for decl in failed_decls or []:
        short = decl.split("::")[-1]
        _add(decl if decl in ranges else (short if short in ranges else ""))
    # 2. line-number fallback (the reliable path on current Verus output)
    for mf, ln in (error_locs or []):
        if not ln or not _loc_in_target(mf, target):
            continue
        for name, s, e in fn_ranges:
            if s <= ln <= e:
                _add(name)
                break

    chunks: list[str] = []
    names: list[str] = []
    for name in ordered:
        s, e = ranges[name]
        body = lines[s - 1:e]
        if len(body) > max_lines_per:
            body = body[:max_lines_per] + ["    // ... (truncated)"]
        chunks.append("\n".join(body))
        names.append(name)
        if len(chunks) >= max_decls:
            break
    return names, "\n\n".join(chunks)


def _diversify_messages(messages: list, cap: int = 24, per_file: int = 4) -> list:
    """Pick up to `cap` diagnostics with cross-FILE coverage: at most `per_file`
    from any single file before moving on, so one noisy module (e.g. edwards.rs)
    can't crowd out every other module's errors. Without this, a whole-crate
    field-floor check that returns 130 errors stored only the first 20 — all
    edwards.rs — and the agent never saw the ristretto/scalar/dep diagnostics.
    Order within a file is preserved; files appear first-seen."""
    from collections import defaultdict
    by_file: dict = defaultdict(list)
    order: list = []
    for m in messages:
        key = m.get("file", "?") if isinstance(m, dict) else "?"
        if key not in by_file:
            order.append(key)
        by_file[key].append(m)
    out: list = []
    for depth in range(per_file):
        for key in order:
            if depth < len(by_file[key]):
                out.append(by_file[key][depth])
                if len(out) >= cap:
                    return out
    return out[:cap]


def _diag_line(m: dict) -> int:
    try:
        return int(m.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def _format_diagnostics_for_memory(messages: list, stderr_tail: str = "") -> str:
    """Render structured diagnostics for retry memory without dropping locations."""
    lines: list[str] = []
    seen: set[str] = set()

    for m in messages or []:
        if isinstance(m, dict):
            data = str(m.get("data") or "").strip()
            file = str(m.get("file") or "").strip()
            line = _diag_line(m)
            try:
                col = int(m.get("column") or 0)
            except (TypeError, ValueError):
                col = 0
            if file and line:
                loc = f"{file}:{line}"
                if col:
                    loc += f":{col}"
                rendered = f"{loc}: {data}" if data else loc
            else:
                rendered = data
        else:
            rendered = str(m).strip()

        if rendered and rendered not in seen:
            seen.add(rendered)
            lines.append(rendered)

    tail = (stderr_tail or "").strip()
    if tail and tail not in seen:
        lines.append(tail)

    return "\n".join(lines).strip()


def _diagnostic_kind(m: object) -> str:
    """Classify Verus diagnostics for metrics and agent feedback.

    Source-span verification errors are the proof queue. Timeouts/build
    wrappers/missing modules are still actionable, but they are not proof-tail
    progress and should not make "errors=1" look like one remaining obligation.
    """
    if not isinstance(m, dict):
        return "meta"
    data = str(m.get("data") or "").lower()
    file = str(m.get("file") or "")
    line = _diag_line(m)
    if "rlimit" in data or "resource limit" in data:
        return "resource-limit"
    if "timed out" in data or "timeout" in data:
        return "timeout"
    if (
        "internal panic" in data
        or "internal error" in data
        or "panicked at" in data
    ):
        return "panic"
    if "could not find module" in data:
        return "missing-module"
    compile_markers = (
        "cannot find",
        "not found in this scope",
        "unresolved import",
        "unresolved name",
        "no method named",
        "no function",
        "cannot find macro",
        "mismatched types",
        "type annotations needed",
    )
    if any(marker in data for marker in compile_markers):
        return "compile"
    if data.startswith("could not compile `") or "previous error" in data:
        return "build-wrapper"
    if file.endswith(".rs") and line > 0:
        return "verification"
    return "meta"


def _verus_error_count(verus_result: dict) -> int:
    """Count raw Verus errors before feedback diversification."""
    raw = verus_result.get("error_count")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    messages = verus_result.get("messages", []) or []
    count = sum(
        1 for m in messages
        if not isinstance(m, dict) or m.get("severity") == "error"
    )
    # If the checker failed before emitting a structured error, do not let that
    # look like a zero-error low.
    if count == 0 and not verus_result.get("okay", False):
        return 1
    return count


def _stored_verus_error_count(messages: list) -> int:
    """Count stored diagnostics that are actual errors, not warnings/notes."""
    return sum(
        1 for m in (messages or [])
        if not isinstance(m, dict) or m.get("severity", "error") == "error"
    )


def _verification_error_count(verus_result: dict) -> int:
    """Count source-span verifier errors only, excluding timeout/build noise."""
    messages = verus_result.get("messages", []) or []
    return sum(
        1 for m in messages
        if (not isinstance(m, dict) or m.get("severity", "error") == "error")
        and _diagnostic_kind(m) == "verification"
    )


def _diagnostic_kind_counts(verus_result: dict) -> dict[str, int]:
    """Count raw error diagnostics by machine-usable kind."""
    counts: dict[str, int] = {}
    for m in verus_result.get("messages", []) or []:
        if isinstance(m, dict) and m.get("severity", "error") != "error":
            continue
        kind = _diagnostic_kind(m)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _round_has_resource_limit_blocker(round_result: RoundResult) -> bool:
    """True when the COMPLETE gate saw an unresolved SMT resource-limit group."""
    counts = round_result.diagnostic_kind_counts or {}
    return counts.get("resource-limit", 0) > 0


def _complete_verus_gate_okay(round_result: Optional[RoundResult]) -> bool:
    """Verus half of COMPLETE.

    A plain green round is enough. A round that still carries rlimit/resource-
    limit diagnostics is not enough, even if a future tiered implementation
    starts recording advisory scoped reruns elsewhere. Claude's firewall
    invariant: every such group must be re-verified green before COMPLETE.
    Until that scoped-rerun proof exists in the round result, block.
    """
    if round_result is None or not round_result.verus_okay:
        return False
    if _round_has_resource_limit_blocker(round_result):
        return False
    return True


def _compile_blocked_or_indeterminate(verus_result: dict) -> bool:
    """True when a failed check lacks source-span proof obligations to score.

    Cargo's generic "could not compile ... previous errors" wrapper appears
    after ordinary proof failures too, so it is not compile-blocking by itself
    when source-span verification diagnostics are present.
    """
    if verus_result.get("okay", False):
        return False
    # A truncated run (verifier died before its final summary — e.g. the
    # vir/src/poly.rs worker panic) has a PARTIAL error list; scoring it as
    # the frontier manufactured a false "only 1 remains" plateau for ~40
    # rounds (field_floor stage3, 2026-07-04). Hold it indeterminate, same
    # as a timeout.
    if verus_result.get("truncated", False):
        return True
    counts = _diagnostic_kind_counts(verus_result)
    if counts.get("compile", 0) or counts.get("missing-module", 0):
        return True
    if _verification_error_count(verus_result) > 0:
        return False
    blocker_kinds = {"timeout", "panic", "build-wrapper", "meta"}
    return bool(blocker_kinds.intersection(counts))


def _plateau_metric_indeterminate(
    experiment_mode: str, admits_left: int, verus_result: dict
) -> bool:
    """True when a failed whole-crate check has no source-span errors to score."""
    return (
        experiment_mode in _WHOLE_CRATE_MODES
        and admits_left == 0
        and not verus_result.get("okay", False)
        and (
            _verification_error_count(verus_result) == 0
            # Truncated runs DO carry source-span errors, but the count is a
            # lower bound of an aborted sweep — not a plateau measurement.
            or verus_result.get("truncated", False)
        )
    )


def _plateau_progress_metric(
    experiment_mode: str, admits_left: int, verus_result: dict
) -> tuple[str, int]:
    """Return the metric the plateau guard should minimize this round."""
    if experiment_mode in _WHOLE_CRATE_MODES and admits_left == 0:
        return (
            "whole-crate source-span Verus errors",
            _verification_error_count(verus_result),
        )
    return ("hard admits", admits_left)


def _merge_plateau_verus_result(base: dict, dep_result: dict) -> None:
    """Add a dep/module check into the plateau metric input.

    Feedback intentionally truncates per-dep diagnostics so the next-round
    prompt stays readable. The plateau metric is different: it should see the
    full gate-scope error count. Otherwise a timed-out whole-crate check can
    look like a one-error near-green while the per-module sweep still has a
    broad tail.
    """
    base.setdefault("messages", []).extend(dep_result.get("messages", []) or [])
    if not dep_result.get("okay", False):
        base["okay"] = False


def _update_plateau_progress(
    current_name: Optional[str],
    current_best: Optional[int],
    rounds_since_new_low: int,
    metric_name: str,
    metric_value: int,
) -> tuple[str, int, int]:
    """Advance plateau state; reset on metric switch or a new low."""
    if current_name != metric_name or current_best is None or metric_value < current_best:
        return metric_name, metric_value, 0
    return current_name, current_best, rounds_since_new_low + 1


def build_admit_inventory_block(
    target: Path, allow_edit: Optional[list[Path]]
) -> str:
    """A per-round inventory of the remaining HARD admits across the gate scope
    (target + allow-edit deps), grouped by file and sorted by count.

    This is the 'where is the work' signal the whole-crate cuts need. Without it,
    a ristretto-anchored field-floor run only sees its own edits and never learns
    the remaining obligations live in shared cone files (curve_equation, straus,
    montgomery_reduce, …) — which is exactly how corefloor_006 plateaued for 32
    rounds while edwards.rs stayed broken and unseen."""
    try:
        cls = classify_remaining_admits(target, allow_edit)
    except OSError:
        return ""
    hard = [d for d in cls.get("detail", [])
            if d.get("classification") == "hard"]
    if not hard:
        return ""
    from collections import defaultdict
    by_file: dict = defaultdict(list)
    for d in hard:
        by_file[d.get("file", "?")].append(d)
    lines = [
        f"### Remaining hard admits: {len(hard)} across {len(by_file)} file(s) "
        f"— WORK THESE",
        "These are the unproven obligations the COMPLETE gate counts. Prioritize "
        "by count. The anchor file may have ZERO — the work is in the dep/cone "
        "files. Do NOT keep re-editing already-clean files.",
        "",
    ]
    def _rel(p: str) -> str:
        # Project-relative-ish path (from "src/" on), so duplicate basenames
        # across dirs (mod.rs, same-named lemma files) stay unambiguous.
        parts = Path(p).parts
        if "src" in parts:
            return "/".join(parts[parts.index("src") + 1:])
        return "/".join(parts[-3:])
    for f in sorted(by_file, key=lambda k: -len(by_file[k])):
        fns = sorted({d.get("function", "?") for d in by_file[f]})
        shown = ", ".join(fns[:6]) + (", …" if len(fns) > 6 else "")
        lines.append(f"- `{_rel(f)}`: {len(by_file[f])}  ({shown})")
    return "\n".join(lines)


def _compact_source_path(path_text: str) -> str:
    """Project-relative-ish path for prompt diagnostics."""
    parts = Path(path_text).parts
    if "src" in parts:
        return "/".join(parts[parts.index("src") + 1:])
    return "/".join(parts[-3:])


def _resolve_diag_file(msg_file: str, work_files: list[Path]) -> Optional[Path]:
    for f in work_files:
        if _loc_in_target(msg_file, f):
            return f
    return None


def _fn_names_in_files(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for f in files:
        try:
            names.update(name for name, _, _ in _fn_ranges_in_file(f))
        except OSError:
            continue
    return names


def _text_references_symbol(text: str, symbols: set[str]) -> bool:
    if not text or not symbols:
        return False
    for sym in symbols:
        if sym and re.search(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])", text):
            return True
    return False


def build_failure_queue_block(
    messages: list,
    work_files: Optional[list[Path]] = None,
    max_source: int = 12,
    max_meta: int = 6,
    truncated: bool = False,
) -> str:
    """Group stored Verus diagnostics into the queue the next agent should work.

    This is intentionally prompt-only: it does not suppress build failures, add a
    gate, or reinterpret COMPLETE. It just prevents a whole-crate timeout/build
    wrapper from being presented as "one proof error left" and points the agent
    at the source-span functions that actually pin the gate.
    """
    work_files = work_files or []
    range_cache: dict[str, list[tuple[str, int, int]]] = {}
    work_symbols = _fn_names_in_files(work_files)
    work_status = {
        str(f): {"path": f, "source_errors": 0, "non_axiom_admits": None}
        for f in work_files
    }
    for row in work_status.values():
        try:
            row["non_axiom_admits"] = _count_llm_target_admits(
                row["path"].read_text())
        except OSError:
            row["non_axiom_admits"] = "?"
    source_groups: dict[tuple[str, str, str], dict] = {}
    meta_rows: list[tuple[str, str, str, int]] = []

    for m in messages or []:
        if isinstance(m, dict) and m.get("severity", "error") != "error":
            continue
        kind = _diagnostic_kind(m)
        if not isinstance(m, dict):
            meta_rows.append((kind, str(m)[:220], "", 0))
            continue
        data = " ".join(str(m.get("data") or "").split())[:220]
        msg_file = str(m.get("file") or "")
        line = _diag_line(m)
        if kind != "verification":
            meta_rows.append((kind, data, msg_file, line))
            continue

        resolved = _resolve_diag_file(msg_file, work_files)
        label = "in_scope_incomplete"
        if resolved is not None:
            status = work_status.get(str(resolved))
            if status is not None:
                status["source_errors"] += 1
        elif _text_references_symbol(data, work_symbols):
            label = "off_lane_caused_by_this_lane"
        else:
            label = "off_lane_other"
        rel = _compact_source_path(str(resolved if resolved is not None else msg_file))
        fn_name = "module"
        if resolved is not None:
            key = str(resolved)
            if key not in range_cache:
                range_cache[key] = _fn_ranges_in_file(resolved)
            fn_name = _enclosing_fn(range_cache[key], line) or "module"
        group = source_groups.setdefault(
            (label, rel, fn_name), {"lines": [], "samples": []}
        )
        group["lines"].append(line)
        if data and data not in group["samples"]:
            group["samples"].append(data)

    if not source_groups and not meta_rows:
        return ""

    out = [
        "### Whole-crate failure queue",
    ]
    if truncated:
        out.append(
            "WARNING: the gate run was TRUNCATED — the verifier aborted before "
            "printing its final summary, so this queue is PARTIAL (a lower "
            "bound, NOT the full frontier). Re-check each editable file with a "
            "module-scoped verus_check before concluding what remains."
        )
    out += [
        "Gate scope, not target-local scope. Work the source-span verification "
        "errors first; fix build/meta diagnostics as blockers, but do not treat "
        "a timeout/build wrapper as one remaining proof obligation.",
        "Labels: `in_scope_incomplete` is an editable-file error; "
        "`off_lane_caused_by_this_lane` is a frozen consumer error whose text "
        "references an editable symbol and stays current-scope; `off_lane_other` "
        "is integration/noise for this run.",
        "",
    ]
    if work_status:
        out.append("Editable-scope status:")
        for key in sorted(work_status, key=lambda k: _compact_source_path(k)):
            row = work_status[key]
            out.append(
                f"- `{_compact_source_path(key)}`: source_errors="
                f"{row['source_errors']}, non_axiom_admits="
                f"{row['non_axiom_admits']}"
            )
        out.append("")

    if source_groups:
        out.append("Source-span verification errors (grouped by scope/function):")
        priority = {
            "in_scope_incomplete": 0,
            "off_lane_caused_by_this_lane": 1,
            "off_lane_other": 2,
        }
        ordered = sorted(
            source_groups.items(),
            key=lambda kv: (
                priority.get(kv[0][0], 9),
                -len(kv[1]["lines"]),
                kv[0][1],
                kv[0][2],
            ),
        )
        for (label, rel, fn_name), info in ordered[:max_source]:
            line_nums = sorted({ln for ln in info["lines"] if ln})
            line_txt = ", ".join(f"L{ln}" for ln in line_nums[:6])
            if len(line_nums) > 6:
                line_txt += ", ..."
            sample = info["samples"][0] if info["samples"] else "verification failed"
            out.append(
                f"- [{label}] `{rel}::{fn_name}`: {len(info['lines'])} error(s)"
                f" ({line_txt}) — {sample}"
            )
        if len(ordered) > max_source:
            out.append(f"- ... {len(ordered) - max_source} more function group(s)")
        out.append("")

    if meta_rows:
        out.append(
            "Meta/build diagnostics (keep visible; do not count as proof-tail progress):"
        )
        for kind, data, msg_file, line in meta_rows[:max_meta]:
            loc = ""
            if msg_file:
                loc = _compact_source_path(msg_file)
                if line:
                    loc += f":{line}"
                loc += ": "
            out.append(f"- {kind}: {loc}{data}")
        if len(meta_rows) > max_meta:
            out.append(f"- ... {len(meta_rows) - max_meta} more meta/build diagnostic(s)")

    return "\n".join(out)


def build_round_history_block(
    tdir: Path, round_num: int, max_recent_rounds: int = 2,
    target: Optional[Path] = None, since_round: int = 1,
    filter_target_errors: bool = True,
    work_files: Optional[list[Path]] = None,
) -> str:
    """Render a markdown block summarizing the previous 1-2 rounds:
    file diffs and verus_check errors. Empty string before round 2.

    Reads:
      - tdir / "snapshots" / "round_<N-1>" / *.rs  (snapshot at end of round N-1)
      - tdir / "snapshots" / "round_<N-2>" / *.rs  (snapshot at end of round N-2, or "round_0" for pre-round-1 baseline)
      - tdir / "round_<N>.json"                    (verus + spec results)
    """
    if round_num <= 1:
        return ""

    import difflib
    snapshots_root = tdir / "snapshots"
    # Clip history range to current session (post-reset) and the recent
    # window. since_round=1 means "all rounds so far"; reset bumps it.
    start_inclusive = max(since_round, round_num - max_recent_rounds, 1)
    history_rounds = list(range(start_inclusive, round_num))

    # Compute the in-progress fn set + line ranges from the LIVE target.
    # Used to drop diff hunks for fully-verified fns (Lever 1).
    in_progress: set[str] = set()
    fn_ranges: list[tuple[str, int, int]] = []
    target_name: Optional[str] = None
    if target is not None and target.exists():
        in_progress = _in_progress_fns(target)
        fn_ranges = _fn_ranges_in_file(target)
        target_name = target.name

    sections: list[str] = []
    for r in history_rounds:
        prev_dir = snapshots_root / f"round_{r - 1}"  # state at END of round r-1 (= start of round r)
        cur_dir = snapshots_root / f"round_{r}"      # state at END of round r
        if not (prev_dir.exists() and cur_dir.exists()):
            continue
        round_json = tdir / f"round_{r}.json"
        verus_okay = None
        verus_errors: list[str] = []
        raw_verus_messages: list = []
        end_reason = None
        gate_truncated = False
        try:
            rr = json.loads(round_json.read_text())
            verus_okay = rr.get("verus_okay")
            end_reason = rr.get("end_reason")
            gate_truncated = bool(rr.get("truncated", False))
            # verus_check stores each diagnostic under "data" (not "message")
            # with file/line/column — render "file:line:col: data" so the agent
            # actually SEES the error. The old m.get("message") always returned
            # "" → blank error fences for every round (the corefloor_006 blind
            # spot). Show more than the prior 5, now that storage is diversified.
            verus_errors = []
            raw_verus_messages = rr.get("verus_errors") or []
            for m in raw_verus_messages[:12]:
                if isinstance(m, dict):
                    loc = (f"{m.get('file','?')}:{m.get('line','?')}:"
                           f"{m.get('column','?')}")
                    verus_errors.append(f"{loc}: {m.get('data','')}"[:400])
                else:
                    verus_errors.append(str(m)[:400])
        except (OSError, json.JSONDecodeError):
            pass

        # Diff each pair of files present in both dirs
        file_diffs: list[str] = []
        all_filtered_empty = True
        cur_files = sorted(cur_dir.glob("*.rs"))
        for cf in cur_files:
            pf = prev_dir / cf.name
            if not pf.exists():
                continue
            try:
                a = pf.read_text().splitlines(keepends=True)
                b = cf.read_text().splitlines(keepends=True)
            except OSError:
                continue
            diff = list(difflib.unified_diff(
                a, b, fromfile=f"round_{r-1}/{cf.name}",
                tofile=f"round_{r}/{cf.name}", n=3,
            ))
            if not diff:
                continue
            blob = "".join(diff)
            # Lever 1 filter: for the target file, drop hunks whose
            # enclosing fn no longer has admit() (verified). Sibling
            # files keep all hunks (new helper lemmas are usually small
            # and relevant to active work). When target_name is unset
            # (no target passed in), no filtering happens.
            if (filter_target_errors and target_name is not None
                    and cf.name == target_name and fn_ranges):
                filtered = _filter_diff_to_in_progress(blob, fn_ranges, in_progress)
                if filtered:
                    blob = filtered
                    all_filtered_empty = False
                else:
                    # Whole file's diff filtered to verified-work-only — skip.
                    continue
            else:
                all_filtered_empty = False
            # Cap each file's diff at ~3000 chars to bound prompt growth
            if len(blob) > 3000:
                blob = blob[:3000] + "\n... (diff truncated, full state on disk)\n"
            file_diffs.append(blob)

        # Filter verus errors to those pointing at in-progress fns (for the
        # target file). Sibling-file errors are always kept. DISABLED for
        # whole-crate cuts (filter_target_errors=False): there, a stripped API
        # proof in the target (e.g. ristretto.rs) fails with NO literal admit()
        # — so an admit()-keyed "in_progress" filter would hide exactly the
        # target failures the agent must see (the corefloor P1 blind spot).
        if filter_target_errors and target_name is not None and fn_ranges:
            def _err_relevant(e: str) -> bool:
                # `e` is a stringified error. Try to parse "file:line".
                m = re.search(r"([\w./-]+):(\d+)", e)
                if not m:
                    return True
                fname, lstr = m.group(1), m.group(2)
                if not fname.endswith(target_name):
                    return True  # sibling/other-file error: keep
                line = int(lstr)
                encl = _enclosing_fn(fn_ranges, line)
                return encl is None or encl in in_progress
            verus_errors = [e for e in verus_errors if _err_relevant(e)]

        failure_queue = ""
        if work_files:
            failure_queue = build_failure_queue_block(
                raw_verus_messages, work_files, truncated=gate_truncated)

        # Render section. If file_diffs is empty AFTER filtering AND
        # there were edits originally, note that. Otherwise standard
        # "no edits" message.
        filter_applied = target_name is not None and bool(fn_ranges)
        diff_note: str
        if file_diffs:
            diff_note = (
                "Edits in this round (filtered to in-progress fns):"
                if filter_applied else
                "Edits in this round:"
            )
        elif all_filtered_empty and filter_applied:
            diff_note = "_Edits this round were inside now-verified fns; nothing in-progress to show._"
        else:
            diff_note = "_No file edits in this round_"

        sections.append("\n".join([
            f"### Round {r} — verus_okay={verus_okay}, end_reason={end_reason}",
            diff_note,
            *([f"```diff\n{d}```" for d in file_diffs] if file_diffs else []),
            failure_queue,
            ("Raw Verus errors (sample):" if verus_errors else ""),
            *([f"```\n{e}\n```" for e in verus_errors] if verus_errors else []),
            "",
        ]))

    if not sections:
        return ""

    repair_hint = (
        "define the missing helper in an editable file allowed by rule 4"
        if work_files else
        "define the missing helper (in the target file, or in a sibling per rule 4)"
    )

    return "\n".join([
        "## Round history (last {} round(s))".format(len(sections)),
        "",
        "What follows is a diff of YOUR previous edits and the Verus errors",
        "that resulted. If a round's edits failed `verus_check.py` and were",
        "reverted, do NOT repeat the same approach — either",
        f"{repair_hint},",
        "or try a different decomposition.",
        "",
        *sections,
    ])


# Bridge rungs: the agent reconstructs deleted shared spec/lemma vocabulary that
# frozen consumers pin. Both get the same run.py treatment (whole-crate verify +
# frozen-file guard); they differ only in how much is stripped and the prompt.
_BRIDGE_MODES = ("bridge-specs", "bridge-full")
# Modes that get the cross-module gate treatment (whole-crate verify + the
# FROZEN_EDIT guard over everything outside the editable set): the bridge rungs
# plus the field-floor cut, whose pins (frozen spec vocabulary + frozen contracts)
# also span the whole crate. Keyed here once so every gate site stays in sync.
_WHOLE_CRATE_MODES = _BRIDGE_MODES + ("field-floor",)

# Whole-crate-mode verus checks (field-floor / bridge-*) verify the full ~2090-fn
# crate and run ~590s in practice; verus_check's default --timeout 300 (sized for
# module checks) fires first and records a misleading "verus timed out" instead of
# the real whole-crate error set, so the harness's verus_okay/feedback diverges
# from what the agent measures with its own `timeout 590 cargo verus verify`. Give
# whole-crate checks a budget well above the observed ~590s.
_WHOLE_CRATE_VERUS_TIMEOUT_SEC = 900


def _round_history_work_files(
    experiment_mode: str,
    experiment_edit_scope: list[Path],
) -> Optional[list[Path]]:
    """Files that round feedback may describe as editable work scope.

    Snapshot targets include frozen witnesses such as the target file; using that
    broader set in the failure queue invites illegal frozen edits. Verus errors in
    frozen files still render, but as off-lane/consumer diagnostics.
    """
    if experiment_mode not in _WHOLE_CRATE_MODES:
        return None
    return list(experiment_edit_scope)


def _should_run_sibling_verify(sibling_verify: bool, experiment_mode: str) -> bool:
    """Whether to run module-scoped sibling re-verification after a round."""
    return bool(sibling_verify) and experiment_mode not in _WHOLE_CRATE_MODES


def _harness_verus_command(
    target: Path,
    project: Path,
    experiment_mode: str,
    verus_rlimit: Optional[float],
) -> list[str]:
    """Build the harness-owned Verus gate command for the round.

    Whole-crate experiment modes intentionally run the first gate at Verus's
    default SMT rlimit. Phase A on sealed 057 showed the global high rlimit can
    turn a completed source-error result into a broad timeout, hiding the real
    queue from the next round.
    """
    cmd = [
        sys.executable,
        str(HERE / "skills" / "verus_check.py"),
        str(target),
        "--project",
        str(project),
    ]
    if experiment_mode in _WHOLE_CRATE_MODES:
        cmd += ["--whole-crate", "--timeout", str(_WHOLE_CRATE_VERUS_TIMEOUT_SEC)]
        return cmd
    if verus_rlimit is not None:
        cmd += ["--rlimit", str(verus_rlimit)]
    return cmd


def _floor_variant_flags(allow_edit: list[Path]) -> tuple[bool, bool]:
    edit_paths = [p.as_posix() for p in allow_edit]
    field_layer_editable = any(
        "/lemmas/field_lemmas/" in s
        or ("/backend/" in s and p.name == "field.rs")
        for p, s in zip(allow_edit, edit_paths)
    )
    trusted_core_editable = any(
        "/lemmas/common_lemmas/" in s
        or "/specs/" in s
        or "/lizard/" in s
        or s.endswith("/src/field.rs")
        or ("/backend/" in s and p.name != "field.rs")
        or p.name in {"scalar_helpers.rs", "traits.rs", "window.rs"}
        for p, s in zip(allow_edit, edit_paths)
    )
    return field_layer_editable, trusted_core_editable


_SCALAR_MONTGOMERY_LANE_RELS = frozenset({
    "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs",
    "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part1_chain_lemmas.rs",
    "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part2_chain_lemmas.rs",
    "curve25519-dalek/src/scalar.rs",
})


def _is_scalar_montgomery_lane_packet(allow_edit: list[Path]) -> bool:
    """True for operator scalar-Montgomery lane/thread editable packets."""
    matched: set[str] = set()
    for path in allow_edit:
        s = path.as_posix()
        for rel in _SCALAR_MONTGOMERY_LANE_RELS:
            if s == rel or s.endswith("/" + rel):
                matched.add(rel)
                break
        else:
            return False
    return bool(matched) and any("/lemmas/" in rel for rel in matched)


def _scalar_montgomery_lane_experiment_block(allow_edit: list[Path]) -> str:
    edit_paths = [p.as_posix() for p in allow_edit]
    lemma_files = [p for p, s in zip(allow_edit, edit_paths) if "/lemmas/" in s]
    api_files = [p for p, s in zip(allow_edit, edit_paths) if "/lemmas/" not in s]
    lemma_bullets = "\n".join(f"- `{p}`" for p in lemma_files) or "- _(none)_"
    api_bullets = "\n".join(f"- `{p}`" for p in api_files) or "- _(none)_"
    edit_bullets = "\n".join(f"- `{p}`" for p in allow_edit) or "- _(none)_"
    verify_command = (
        "python3 {SKILLS_ROOT}/verus_check.py {TARGET_PATH} "
        "--project {PROJECT_ROOT} --whole-crate"
    )
    return f"""## EXPERIMENT MODE -- read this first

This is the **field-floor scalar-Montgomery lane** rung -- a lane-isolated
field-floor run. The field-floor cone is the surrounding experiment, but your
assignment is ONLY the scalar Montgomery reduction lane. This lane section wins
over any later generic wording that says "whole editable assignment" or "whole
cone".

**This is BY DESIGN, not a corrupted workspace.** Many off-lane proofs may be
missing, stubbed, or noisy. They are not your current task. Do not reconstruct
Pippenger/Straus/digit/radix/NAF, Edwards, Ristretto, or unrelated scalar APIs
unless you can name the direct dependency path back to a current-lane Montgomery
contract or proof.

**Editable lane/thread files (the ONLY files you may touch in this run):**
{edit_bullets}

**Lane lemma files:** deleted Montgomery-reduction proof functions must be
re-created with signatures, contracts, and proof bodies strong enough for their
frozen scalar/backend consumers:
{lemma_bullets}

**Lane API file:** `scalar.rs` is editable for scalar-Montgomery proof blocks,
local proof-only assertions, and small helper calls that discharge
`montgomery_reduce` / `from_montgomery` obligations. Scalar API proof blocks are allowed when they trace to this lane; arbitrary scalar API cleanup is not.
{api_bullets}

**Frozen/off-lane substrate:** every file outside the editable lane list is
frozen. If this run starts with operator-owned off-lane compile-debt stubs, they
are a frozen compile shield, out of scope, and non-scoreable. Do not expand,
repair, count, or chase those stubs. They only isolate current-lane signal and
still require a later de-stubbed whole-crate integration gate before any lane
bank can be claimed as a result.

**Lane acceptance, not crate banking:** a scoped module/file green is only
steering signal. Do not call the lane done until every editable lane/thread file
listed above has zero non-axiom admits and zero in-scope source errors under the
default whole-crate feedback. That is a lane-local `proof_delta`, not
`BANKED_COMPLETE`.

**Prior Montgomery proofs are seed evidence, not a cache:** if the operator
points you at previously proven Montgomery material, use it as proof ideas and
cross-check evidence only. Re-verify every reused proof against the current
editable files, current contracts, and current whole-crate feedback; never skip
the final lane check because an earlier run was green.

**Convergence framing:** this is assisted convergence with operator
decomposition handoffs. It does not refute any Section 11 unaided-convergence
ceiling; the open question here is whether assisted lane work can produce a
real admit-free `proof_delta` that later composes into a de-stubbed integration
bank.

**What pins your reconstruction:**
- Frozen scalar/backend callers decide whether each reconstructed lemma contract
  is strong enough and true.
- Existing API contracts, exec code, and spec definitions are frozen by the
  gates. Do not weaken them.
- Generated or reconstructed `lemma_*` contracts in the editable lane lemma
  files are proof-synthesis outputs. You may revise their `requires`,
  `ensures`, or `decreases` when caller signal shows the contract is too weak,
  too strong, or false.
- Reduced-but-nonzero admits are partial progress, never completion. COMPLETE
  requires whole-crate green and zero non-axiom admits in the gate scope.

**Rules (override the standard rules below):**
- Edit ONLY the lane/thread files listed above. Everything else is frozen.
- Write only proof artifacts: re-created deleted lemmas, stripped lane-relevant
  `scalar.rs` proof blocks, proof-only assertions, and new helper `proof fn`
  lemmas in the editable lane files.
- Minimal `admit()` bodies are allowed only as temporary compile-debt for a
  current unresolved lane callsite. They are not proof progress and they block
  COMPLETE until discharged. Do not use `assume(...)`,
  `#[verifier::external_body]`, or new `proof fn axiom_*`.
- Use scoped Montgomery/scalar checks for steering. Use the whole-crate command
  as the reconciliation/bank gate, not as permission to chase unrelated errors:
  `{verify_command}`.
- `END_REASON:COMPLETE` only when the gate scope verifies with zero non-axiom
  admits. A lane green with operator stubs is steering signal until the later
  de-stubbed whole-crate gate passes.

**Single-thread convergence rule:** if this packet lists fewer than the full
four-file scalar-Montgomery lane, that is intentional. Do not open sibling lane
files just to build a broad scaffold. Bank one current proof thread in the
listed file(s) first. A lower error count obtained by adding several new admits
is compile debt, not proof progress; real progress is one verified leaf, one
removed admit, or one contract repair that survives the next same-scope check
without creating new admits.

**Natural-language scheduler (strict):** at every verifier boundary, choose the
next action by this order; do not skip a step unless the verifier output makes
that step impossible. (1) If the verifier cannot resolve a deleted proof
function, discard off-lane blockers under the lane filter, then re-create only
the minimal signature + contract for the current unresolved lane callsite.
(2) Re-run the same verifier command or a narrower Montgomery lane check when
the previous command was dominated by off-lane blockers. (3) Trace the immediate
dependency chain from the frozen scalar/backend caller down through editable
Montgomery lemmas. (4) Pick the lowest unproved dependency whose non-frozen
callees are already proved, and bank that one proof thread before moving upward.
(5) If that thread blocks, decompose only its immediate prerequisite and return
to the same thread. This is a scheduler, not optional advice.
"""


def _floor_reconstruction_prompt(
    *,
    rung_title: str,
    scope_sentence: str,
    by_design_body: str,
    scope_block: str,
    reconstruction_block: str,
    frozen_block: str,
    pins_block: str,
    write_rule: str,
    verify_command: str,
    extra_rules: list[str] | None = None,
) -> str:
    extra_rules_text = ""
    if extra_rules:
        extra_rules_text = "".join(f"- {rule}\n" for rule in extra_rules)
    return f"""## EXPERIMENT MODE -- read this first

This is the **{rung_title}** rung -- {scope_sentence} The
standard rules below mostly apply; this section adjusts them and wins where they
conflict.

**This is BY DESIGN, not a corrupted workspace.** Many proofs are missing and the
crate may not compile -- that is the intended starting state, not damage.
{by_design_body}

{scope_block}

{reconstruction_block}

{frozen_block}

**What pins your reconstruction (why this is sound, and hard):**
{pins_block}

**Rules (override the standard rules below):**
- **Edit ONLY the files listed above.** Every other file is FROZEN; editing any
  fails the round as FROZEN_EDIT and can never be COMPLETE.
- {write_rule}
- Do not modify Rust *exec* code -- bodies, signatures, types, imports are
  correct as written.
- No `admit()`, `assume(...)`, `#[verifier::external_body]`, no new `proof fn
  axiom_*`.
- **Reconstruct from the code in front of you -- do NOT recover the originals
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`, no
  reading `.git/`. Copying pre-strip proofs back is retrieval, not
  reconstruction.
{extra_rules_text}- **Verify the WHOLE crate** (`{verify_command}`): the pins span many
  modules; a per-module check proves nothing.
- `END_REASON:COMPLETE` only when the whole crate verifies with zero admits.
  Partial progress is expected and valuable; never fake a green.
"""


def _deeper_floor_experiment_block(
    allow_edit: list[Path],
    *,
    field_layer_editable: bool,
    trusted_core_editable: bool,
) -> str:
    if not (field_layer_editable or trusted_core_editable):
        raise ValueError("deeper floor prompt requires field or trusted-core edits")

    edit_bullets = "\n".join(f"- `{p}`" for p in allow_edit) or "- _(none)_"
    rung_title = "trusted-core" if trusted_core_editable else "number-theory-floor"
    scope_sentence = (
        "the broad trusted-core experiment. The entire in-repo proof layer has "
        "been peeled. You reconstruct proofs above frozen spec definitions, "
        "exec contracts/code, axioms/assumes, and external vstd."
        if trusted_core_editable else
        "the broad number-theory-floor experiment. The field proof layer has "
        "also been peeled. You reconstruct the proof cone above the L5 "
        "number-theory/common substrate, including L4 field lemmas and backend "
        "field proof blocks."
    )
    variant_block = (
        """**Entire in-repo proof layer peeled** -- this is the trusted-core variant.
The editable list may include common number-theory lemmas, field lemmas,
spec-module proof lemmas, backend files, helper files, and top-level API files.
For each listed file, reconstruct the deleted proof fns and stripped proof
blocks. Existing `spec fn` definitions are frozen by the spec-definition gate;
do not rewrite the mathematical vocabulary, but you MAY add new real helper
`proof fn` lemmas in editable spec files."""
        if trusted_core_editable else
        """**Field layer also peeled** -- this is the number-theory-floor variant.
Editable field lemma files had proof fns deleted, and editable backend field
files had proof blocks stripped. Rebuild those field proofs too. Field spec
definitions stay frozen; prove the implementation and lemmas against that
vocabulary."""
    )
    frozen_floor = (
        "External vstd plus the trusted axiom/assume/external-body floor. "
        "Existing spec definitions are frozen by `--check-spec-defs`; existing "
        "fn headers/contracts and exec code are frozen by the spec/diff gates."
        if trusted_core_editable else
        "The L5 number-theory/common substrate (`common_lemmas/*`) plus vstd "
        "and the trusted axiom/assume/external-body floor. Field spec "
        "definitions are frozen; editable field lemma/backend files are proof "
        "reconstruction targets, not spec targets."
    )
    return _floor_reconstruction_prompt(
        rung_title=rung_title,
        scope_sentence=scope_sentence,
        by_design_body=(
            "Do not report the tree as broken/out-of-scope, and do not assume "
            "anyone else is fixing part of it: **you are the only agent, and "
            "every editable file below is yours to complete.**"
        ),
        scope_block=f"""**The editable list is the COMPLETE source of truth.** Do not infer frozen scope
from the floor name. Files in this list are editable proof-reconstruction
targets; files outside it are frozen.

**Editable files (the ONLY files you may touch):**
{edit_bullets}

{variant_block}""",
        reconstruction_block="""**What was peeled:** reconstruct removed proofs in every listed file. This can
mean re-created deleted `proof fn` lemmas (with signatures/contracts strong
enough for their callsites), stripped inline `proof { ... }` blocks, and
proof-only assertions. Add NEW helper `proof fn` lemmas when useful.

On a resume, a previously reconstructed `lemma_*` may already exist in the
worktree. If it is in an editable `src/lemmas/**` file and its generated
contract is too weak, too strong, or false, it is still yours to repair: revise
its header / `requires` / `ensures` / `decreases` instead of treating it as a
frozen false contract.""",
        frozen_block=f"**Frozen substrate:** {frozen_floor}",
        pins_block="""- Existing API contracts and original fn headers (`requires`/`ensures`/
  `decreases`) are FROZEN by the spec gate -- one edit fails the round as
  SPEC_DRIFT even if Verus turns green.
- Existing `spec fn` definitions are FROZEN by the spec-definition gate. Their
  bodies are the mathematical vocabulary; use them, do not change them.
- Generated or reconstructed `lemma_*` contracts in editable lemma files are NOT
  frozen, even if they already exist on a resumed worktree. Verus callers and the
  frozen API/exec contracts decide whether your chosen contracts are strong
  enough and true.
- Newly added helper `proof fn` lemmas are allowed in editable files, including
  editable `specs/*` modules in trusted-core, but they must be real proofs.
- A Verus error reported in a frozen file may still be caused by a too-weak
  generated contract in an editable dependency. Trace the called lemmas and
  repair the editable proof/contract before declaring the frozen proof out of
  scope.""",
        write_rule=(
            "Write ONLY proofs: re-created deleted lemmas, stripped proof "
            "bodies, and NEW helper `proof fn`s. You MAY revise `lemma_*` "
            "headers / `requires` / `ensures` / `decreases` in editable "
            "lemma files because those contracts were generated by this "
            "experiment, including on resumes. Do NOT add or change any "
            "`spec fn`, and do NOT edit API/backend exec headers / `requires` "
            "/ `ensures` / `decreases`."
        ),
        verify_command="python3 {SKILLS_ROOT}/verus_check.py {TARGET_PATH} --project {PROJECT_ROOT} --whole-crate",
    )


def _field_floor_experiment_block(allow_edit: list[Path]) -> str:
    """Prompt for the field-floor cut (peel.py --classify-floor field): the whole
    above-field correctness cone is gutted -- every non-axiom proof fn in the
    editable lemma files DELETED and every listed API file inline-proof STRIPPED.
    Data-driven on the editable set (no hardcoded lemma list), so it states the
    true scope instead of the curated decompress text (which claimed only ~10
    named lemmas deleted and ristretto_lemmas frozen, making the agent declare the
    tree corrupted and invent other workers -- peel_corefloor_002/003).
    Deeper floor manifests still dispatch as mode="field-floor"; detect them
    from the editable set before rendering field-specific frozen-scope text."""
    if _is_scalar_montgomery_lane_packet(allow_edit):
        return _scalar_montgomery_lane_experiment_block(allow_edit)

    field_layer_editable, trusted_core_editable = _floor_variant_flags(allow_edit)
    if field_layer_editable or trusted_core_editable:
        return _deeper_floor_experiment_block(
            allow_edit,
            field_layer_editable=field_layer_editable,
            trusted_core_editable=trusted_core_editable,
        )

    edit_paths = [p.as_posix() for p in allow_edit]
    lemma_files = [p for p, s in zip(allow_edit, edit_paths) if "/lemmas/" in s]
    api_files = [p for p, s in zip(allow_edit, edit_paths) if "/lemmas/" not in s]
    lemma_bullets = "\n".join(f"- `{p}`" for p in lemma_files) or "- _(none)_"
    api_bullets = "\n".join(f"- `{p}`" for p in api_files) or "- _(none)_"
    return _floor_reconstruction_prompt(
        rung_title="field-floor",
        scope_sentence=(
            "the hardest and BROADEST. The ENTIRE proof cone above the field "
            "layer has been removed and you reconstruct ALL of it."
        ),
        by_design_body=(
            "Do not report the tree as broken/out-of-scope, and do not assume "
            "anyone else is fixing part of it: **you are the only agent, and "
            "EVERY editable file below is yours to complete.** There are no "
            "other workers and no narrow named subset -- the whole cone is the "
            "task."
        ),
        scope_block="""Ignore any prior memory, discovery note, or failed-attempt note that says this is
a narrow target-local task or that editable API files are out of scope. In this
field-floor run, the editable list below wins: top-level API proof bodies are in
scope, and only their contracts are frozen.

**The editable list is the COMPLETE source of truth.** Two transforms were
applied; reconstruct everything they removed, using only the frozen substrate:""",
        reconstruction_block=f"""**(A) Correctness lemmas DELETED entirely** (signature + contract + body) from
these files. Each was called by a frozen proof or a kept lemma, so the crate does
not compile until you re-create each one: a signature its callsites accept, a
`requires`/`ensures` strong enough for every caller, and a proof -- you may draft
the body with `admit()` to get the crate compiling and expose real signal, but
COMPLETE requires every such admit discharged into a real proof. You choose
the decomposition; add whatever NEW helper lemmas you need. These generated
`lemma_*` signatures and contracts are yours to revise as proof synthesis
progresses -- if a reconstructed `requires` / `ensures` is too weak, too strong,
or false, fix it instead of freezing it as an obstacle. Reconstruct **every**
non-axiom `proof fn` these files are missing (the originals had dozens per file --
do not stop at a handful):
{lemma_bullets}

**(B) API proof bodies STRIPPED** (signature + `requires`/`ensures` + exec code
kept byte-identical; only inline `proof {{ ... }}` blocks and proof-only `assert`s
removed, so `cargo verus` fails them with *"postcondition not satisfied"*).
Rewrite every stripped proof in:
{api_bullets}""",
        frozen_block="""**EVERYTHING below the field layer is frozen** -- and NOT editable:
- EVERY spec definition (`specs/*`: field/edwards/montgomery/ristretto/scalar
  specs and the Montgomery<->Edwards bridge map). Frozen by the spec-definition
  gate -- your proofs are written in this vocabulary but may not redefine it.
- The L4 field proof layer (`lemmas/field_lemmas/*`) and the L5 number-theory /
  `common_lemmas/*` substrate, the backend, and external vstd.
- Every `axiom_*` / `assume` / `#[verifier::external_body]` -- call them, never
  add or redefine them.""",
        pins_block="""- All API contracts (`fn` headers + `requires`/`ensures`/`decreases` in the
  editable API files) are FROZEN by the spec gate -- one edit fails the round as
  SPEC_DRIFT even if Verus turns green.
- The generated `lemma_*` contracts in editable lemma files are NOT frozen; they
  are proof-synthesis outputs. Verus callers and the frozen API contracts decide
  whether your chosen contracts are strong enough and true.
- Frozen API contracts are written only in frozen spec vocabulary, so a bad
  re-created lemma contract merely fails to discharge a frozen `ensures`
  (-> not COMPLETE), never a silent weakening.
- A Verus error reported in a frozen file may still be caused by a too-weak
  generated contract in an editable dependency. Trace the called lemmas and
  strengthen or repair the editable contract before declaring the frozen proof
  out of scope.""",
        write_rule=(
            "Write ONLY proof artifacts: re-created deleted lemmas (each with "
            "a contract -- you MAY draft the body with `admit()` to unblock "
            "compile, then discharge it leaf-first), the stripped API proof "
            "bodies, and NEW helper `proof fn`s. You MAY revise `lemma_*` "
            "headers / `requires` / `ensures` / "
            "`decreases` in the editable lemma files because those contracts "
            "were generated by this experiment. Do NOT add or change any `spec "
            "fn`, and do NOT edit API headers / `requires` / `ensures` / "
            "`decreases`."
        ),
        verify_command="python3 {SKILLS_ROOT}/verus_check.py {TARGET_PATH} --project {PROJECT_ROOT} --whole-crate",
        extra_rules=[
            "**Current lane packet (strict):** for this run, treat the target "
            "file only as a harness anchor. Your current proof lane is exactly "
            "the editable list above plus frozen consumers whose obligations "
            "trace directly to those editable files. Work one proof thread at "
            "a time. Reconstruct contracts from frozen callers and sibling "
            "uses; prove the lowest prerequisites first; use scoped checks on "
            "the editable files and their direct consumers for steering. Do "
            "not chase unrelated APIs or broad whole-crate buckets while the "
            "current lane has active work. A downstream error is lane signal "
            "only if its dependency trace reaches a current editable contract "
            "or proof; fix that. Otherwise record it as integration or "
            "next-lane signal. Reduced-but-nonzero admits are partial progress, "
            "not completion; the scheduler must return to that lane. COMPLETE "
            "requires whole-crate green and zero non-axiom admits.",
            "**Lane bank and integration rhythm:** after a full or partial "
            "bank, run a deliberate whole-crate reconciliation pass. Fix only "
            "traced lane-contract seams or small local proof-block repairs, "
            "then return to focused lane work. A genuine blocker is not 'this "
            "proof is hard': it means no admit-free path is visible only after "
            "you have re-derived the leaf contract from frozen consumers and "
            "siblings, identified immediate prerequisites, attempted those "
            "prerequisites in dependency order, and checked whether a small "
            "local consumer proof-block repair would discharge the "
            "obligation.",
            "**Do not create an off-lane stub profile on this first attempt.** "
            "Stubs can mask the flawed-contract failure mode: admitted "
            "off-lane consumers stop exercising the current lane contract, so a "
            "too-weak `ensures` can look banked against a stubbed surface. If "
            "an operator later supplies an explicit stub profile, treat it only "
            "as signal isolation; a de-stubbed whole-crate integration check "
            "must gate any lane bank.",
            "**If this run starts with operator-owned off-lane compile-debt "
            "stubs already present, leave them alone.** They exist only to "
            "keep off-lane noise from blocking the current lane. Do not expand, "
            "repair, count, or chase those stubs; use them only to reach "
            "current-lane checks. They still block COMPLETE until a later "
            "de-stubbed whole-crate integration gate passes.",
            "**Lane filter comes before compile resolution.** 'Current "
            "unresolved callsite' means a callsite whose dependency trace "
            "reaches one of the editable files above, not every error printed "
            "by a broad module or whole-crate check. If a verifier boundary "
            "reports missing or unproved off-lane proof functions, do not "
            "reconstruct them in this run. Record them as next-lane compile "
            "debt, choose a narrower current-lane check if needed, and continue "
            "with the lowest current-lane admit or obligation. Only touch an "
            "off-lane proof block when you can name the direct dependency path "
            "from that block to a current-lane contract/proof.",
            "**Strategy for a cut this broad:** first get the crate to COMPILE "
            "-- re-create the deleted lemma signatures + contracts so callsites "
            "resolve -- then discharge the proofs, but apply the lane filter "
            "above before deciding which callsites are current. Keep exploration in the main "
            "proof session using Read/Bash; do not spawn helper agents or "
            "background global pollers.",
            "**Natural-language scheduler (strict):** at every verifier "
            "boundary, choose the next action by this order; do not skip a step "
            "unless the verifier output makes that step impossible. (1) If the "
            "verifier cannot resolve a deleted proof function, first discard "
            "off-lane blockers under the lane filter, then re-create only the "
            "minimal signature + contract for the current unresolved lane "
            "callsites. (2) Re-run the same verifier command or a narrower "
            "lane check when the previous command was dominated by off-lane "
            "blockers. (3) From the new "
            "errors, trace the immediate dependency chain from the frozen "
            "caller down through editable lemmas. (4) Pick the lowest unproved "
            "dependency whose non-frozen callees are already proved, and bank "
            "that one proof thread before moving upward. (5) If that thread "
            "blocks, decompose only its immediate prerequisite and return to "
            "the same thread. This is a scheduler, not optional advice.",
            "**Compile resolution is not proof progress:** if verification stops "
            "because a deleted proof function cannot be resolved, re-create only "
            "the minimal lane-filtered signature and contract needed by the "
            "caller, even if that missing function is a high-level consumer. "
            "This compile-debt "
            "prelude may be the first necessary step before any real leaf signal "
            "is visible. Then run the same verifier command again. An unproved "
            "body or a too-weak contract is debt, not a banked proof.",
            "**Stay on the discovered thread:** after a compile blocker resolves "
            "or a leaf proof is banked, follow the newly exposed closest "
            "dependency. `admit()` is allowed as draft / compile debt -- including "
            "a high-level consumer signature forced by an unresolved callsite -- "
            "but only as debt: keep it minimal to the current unresolved "
            "callsites, never count it as proof progress, and it blocks "
            "COMPLETE until discharged one thread at a time, leaf-first. "
            "Off-schedule: "
            "switching into a broad consumer proof campaign, sprawling new "
            "admits across the cone, using a lower visible error count to justify "
            "leaving multiple new admits unresolved, or treating unrelated helper "
            "blocks as progress."
        ],
    )


def build_experiment_block(
    target: Path, allow_edit: list[Path], mode: str = "spec-proof"
) -> str:
    """Render the experiment-mode prompt addendum. Empty string if not in
    experiment mode (no allow_edit paths).

    `mode` selects which experimental setup the agent is in:
      - "spec-proof": dep fns have no Verus specs; agent infers
        requires/ensures/decreases AND adds proof scaffolding. Helper
        lemmas may be added.
      - "proof-only": specs are fixed; agent only adds proof scaffolding
        inside existing fn/lemma signatures. No new lemmas, no new
        axioms, no new lemma skeletons.
      - "contract-only": the anchor's CONTRACT (signature + requires/
        ensures/decreases) is frozen and gate-protected, but its PROOF
        BODY was stripped and its helper lemmas deleted. The agent
        rewrites the anchor's proof body AND invents the helper lemmas
        from scratch. The hardest rung — see the --no-anchor-proof flag.
    """
    if not allow_edit:
        return ""
    bullets = "\n".join(f"- `{p}`" for p in allow_edit)
    if mode == "contract-only":
        deps = [p for p in allow_edit if p.resolve() != target.resolve()]
        dep_bullets = "\n".join(f"- `{p}`" for p in deps) or "- _(none)_"
        return f"""## EXPERIMENT MODE — read this first

This is the contract-only rung. The standard rules below mostly apply;
this section adjusts them and wins where they conflict.

**Anchor — editable PROOF, FROZEN CONTRACT:** `{target}`

The anchor's top-level `pub fn` has its **signature, `requires`, `ensures`,
and `decreases` FROZEN** — that is the user-visible API contract, and it is
the *only* thing that makes this run sound. Its own orchestration proof body
(the inline `proof {{ ... }}` blocks and proof-only `assert`s) has been
**stripped**, so `cargo verus` now fails on the anchor with *"postcondition
not satisfied"*. You MAY rewrite that proof body — add `proof {{ ... }}`
blocks, `assert(...)`, `assert(...) by (...)`, ghost bindings — and you MAY
edit the helper-lemma file below.

**You MUST NOT change any `fn` header, `requires`, `ensures`, or `decreases`
anywhere in the anchor file** (not on the anchor fn, not on its `step_*`
sub-functions). **The spec-integrity gate is ON**: a single edit to any frozen
contract clause fails the round as SPEC_DRIFT and cannot be promoted to
COMPLETE — even if Verus turns green. Re-phrasing, weakening, or deleting an
`ensures` is precisely the cheat the gate exists to catch.

**Helper-lemma file (deleted — rebuild from scratch):**
{dep_bullets}

The decompress helper lemmas that the anchor's proof used to call have been
**deleted**. Invent your own — signatures, `requires`/`ensures`, and proofs —
strong enough to discharge the anchor's frozen `ensures` from the
`step_1`/`step_2` postconditions (whose contracts are intact). **You need not
reproduce the original lemmas**; choose your own decomposition.

**You have full creative freedom over proof structure.** Helper lemmas,
broadcast, assert-by, reveal_with_fuel, ghost variables — all permitted, in
the anchor's proof body or the helper file.

**Rules (override the standard rules below):**
- Edit only the files listed above (the anchor's proof body + the helper file).
  Do not modify the anchor's contract clauses or any other module.
- Do not modify any Rust *exec* code — exec bodies, exec fn signatures, types,
  imports are correct as written. Your edits add Verus annotations + proofs.
- No `admit()`, `assume(...)`, or `#[verifier::external_body]`.
- No new `proof fn axiom_*` declarations.
- **Reconstruct from the code in front of you — do NOT recover the original
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. The worktree's history still holds the pre-strip proof
  and lemmas; copying them back is retrieval, not reconstruction — it fails
  the round (GIT_RECOVERY) and is never COMPLETE.
- **Run `python3 {{SKILLS_ROOT}}/verus_check.py <absolute-file.rs> --project {{PROJECT_ROOT}}`
  on the anchor AND on the helper file separately** (`--verify-module` is
  per-module). All checks must return `okay: true`.
- `END_REASON:COMPLETE` only when every check passes with zero admits.
"""
    if mode == "field-floor":
        return _field_floor_experiment_block(allow_edit)
    if mode == "bridge-full" and target.stem == "ristretto" and len(allow_edit) > 1:
        # The full-stack rung (--no-fullstack-proof): the WHOLE decompress proof
        # tree across all THREE layers is reconstructed at once — ristretto
        # (decompress + step_1/step_2 proofs), edwards (decompress proof),
        # montgomery (to_edwards proof), plus 10 deleted decompress-path lemmas.
        # Target is ristretto.rs but the editable set is the 5 files. Render a
        # combined prompt; the generic edwards-only and pure-ristretto blocks
        # would each under-describe it.
        field_layer_editable, trusted_core_editable = _floor_variant_flags(allow_edit)
        rung_title = (
            "trusted-core" if trusted_core_editable else
            "number-theory-floor" if field_layer_editable else
            "full-stack"
        )
        rung_intro = (
            "You reconstruct every in-repo proof artifact above frozen spec "
            "definitions, exec contracts/code, axioms/assumes, and external vstd."
            if trusted_core_editable else
            "You reconstruct the ENTIRE proof cone above the L5 number-theory "
            "floor, including the L4 field proof layer."
            if field_layer_editable else
            "You reconstruct the ENTIRE decompress proof tree across all THREE "
            "API layers at once."
        )
        field_layer_block = """

**(C) Field layer also peeled** — this is the number-theory-floor variant.
`lemmas/field_lemmas/*` proof fns listed in the editable set were deleted, and
backend `field.rs` proof blocks were stripped. Rebuild those field proofs too.
The field spec definitions (`specs/field_specs*`) stay frozen; do not redefine
the vocabulary, only prove the implementation and lemmas against it.
""" if field_layer_editable and not trusted_core_editable else ""
        trusted_core_block = """

**(D) Entire in-repo proof layer peeled** — this is the trusted-core variant. The
editable list is the source of truth: it may include common number-theory lemmas,
field lemmas, loose scalar/montgomery/lizard lemmas, spec-module proof lemmas,
backend files, and helper files. For each listed file, reconstruct only the
deleted proof fns and stripped proof blocks. Existing `spec fn` definitions are
frozen by the spec-definition gate; do not rewrite the mathematical vocabulary.
External vstd is frozen substrate and is never peeled.
""" if trusted_core_editable else ""
        freeze_note = (
            "Do NOT look outside the editable list for more deletions. Existing "
            "spec definitions, all fn headers/contracts, and all exec code are "
            "frozen even when they live in editable files."
            if trusted_core_editable else
            "Do NOT look for other deletions or missing spec fns; there are none."
        )
        frozen_floor = (
            "- External vstd plus the trusted axiom/assume/external-body floor. "
            "Existing spec definitions are frozen by the spec-definition gate; "
            "exec contracts and code are frozen by the header gate and diff guard."
            if trusted_core_editable else
            "- The L5 number-theory/common substrate (`common_lemmas/*`) and vstd.\n"
            "  The field spec vocabulary is frozen too; the editable field lemma / "
            "backend files are proof-reconstruction targets, not spec targets."
            if field_layer_editable else
            "- The whole field / number-theory substrate (`field_lemmas/*`, "
            "`common_lemmas/*`) and vstd."
        )
        return f"""## EXPERIMENT MODE — read this first

This is the **{rung_title}** rung — the hardest. {rung_intro} The standard
rules below mostly apply; this section adjusts them and wins where they conflict.

**Editable files (the ONLY files you may touch):**
{bullets}

Decompression spans three layers, each built on the one below:
`CompressedRistretto::decompress` (ristretto.rs) → the Edwards point-decompress
path (`edwards.rs::decompress`, and `MontgomeryPoint::to_edwards` in
montgomery.rs) → the field/number-theory substrate. Two things were done to the
editable files:

**(A) Proof bodies STRIPPED** (signature + `requires`/`ensures` + executable code
all kept byte-identical; only the inline `proof {{ ... }}` blocks and proof-only
`assert`s removed, so `cargo verus` fails these with *"postcondition not
satisfied"*). Rewrite each proof:
- `ristretto.rs`: `CompressedRistretto::decompress` and its two `mod decompress`
  helpers `step_1` and `step_2`.
- `edwards.rs`: `decompress`.
- `montgomery.rs`: `to_edwards`.

**(B) Lemmas DELETED entirely** (signature + contract + body) — the frozen-ish
callers no longer compile until you re-create each: signature (fixed by the
callsites), a `requires`/`ensures` strong enough for every caller, and a real
proof. Add whatever NEW helper lemmas you need.
- `lemmas/edwards_lemmas/decompress_lemmas.rs`: `lemma_decompress_valid_branch`,
  `lemma_to_edwards_correctness`, `lemma_decompress_field_element_sign_bit`,
  `lemma_decompress_spec_matches_point`, `lemma_sign_bit_after_conditional_negate`.
- `lemmas/edwards_lemmas/curve_equation_lemmas.rs`: `lemma_negation_preserves_curve`,
  `lemma_affine_to_extended_valid`, `lemma_edwards_affine_when_z_is_one`,
  `lemma_x_zero_implies_y_squared_one`, `lemma_unique_x_with_parity` (pure curve
  facts you must re-derive, e.g. "x = 0 on the curve ⟹ y² = 1", "the parity fixes
  a unique x").
{field_layer_block}
{trusted_core_block}

**EVERYTHING else is frozen** — and NOT editable. {freeze_note} Frozen and correct:
- EVERY spec definition: the Montgomery↔Edwards map
  (`montgomery_to_edwards_affine`, `edwards_y_from_montgomery_u` in the bridge
  module), `ristretto_specs` (`spec_ristretto_decompress`, `ristretto_decode_*`,
  `is_in_even_subgroup`), `edwards_specs`, `montgomery_specs`, `field_specs`.
- The ristretto lemma layer `lemmas/ristretto_lemmas/*` including the **axioms**
  (`axiom_ristretto_decode_on_curve`, `axiom_ristretto_decode_in_even_subgroup`)
  — your proofs CALL them but must not redefine them.
{frozen_floor}

**Leave unrelated code in the editable files alone.** The editable `.rs` files
also hold fully-proven, unrelated APIs and their own `open spec fn`s
(`well_formed`, `eq_spec`, `add_spec`, `from_spec`, `neg_spec`, …) plus, in the
lemma files, unrelated group-law/field lemmas. Do NOT touch any unrelated item —
their contracts are gate-frozen, their proofs pass, and their specs are consumed
by frozen callers. You write proofs ONLY inside the stripped functions + the
re-created deleted lemmas + your own NEW helper `proof fn`s.

**What pins your reconstruction (why this is sound, and hard):**
- All five API contracts (`ristretto::decompress`/`step_1`/`step_2`,
  `edwards::decompress`, `montgomery::to_edwards`) are FROZEN by the spec gate
  (it snapshots every fn header in every editable file). One edit to any of them
  fails the round as SPEC_DRIFT — even if Verus turns green.
- Those contracts are written ONLY in frozen spec vocabulary (all in files you
  may NOT edit), so the user-facing meaning is fixed no matter what helper lemmas
  you choose. A too-weak lemma merely fails to discharge a frozen `ensures`
  (→ not COMPLETE), never a silent weakening.
- Each deleted lemma is called by a frozen proof or kept lemma, so your re-created
  version needs a compatible signature AND a contract strong enough for every
  caller; too weak → the whole crate fails to verify.

**Rules (override the standard rules below):**
- **Edit ONLY the files listed above.** Every other file is FROZEN; editing
  any of them fails the round as FROZEN_EDIT and can never be COMPLETE.
- Write ONLY proofs: the stripped proof bodies, the re-created deleted lemmas
  (each with a contract), and NEW helper `proof fn`s. You may NOT add or change
  any `spec fn`, nor edit any `fn` header / `requires` / `ensures` / `decreases`,
  nor touch the unrelated APIs / group-law lemmas.
- Do not modify any Rust *exec* code — exec bodies, signatures, types, imports
  are correct as written. Your edits add Verus annotations + proofs only.
- No `admit()`, `assume(...)`, `#[verifier::external_body]`, no new
  `proof fn axiom_*`.
- **Reconstruct from the code in front of you — do NOT recover the originals from
  version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`, no
  reading `.git/`. Copying the pre-strip proofs/lemmas back is retrieval, not
  reconstruction — it fails the round (GIT_RECOVERY) and is never COMPLETE.
- **Verify the WHOLE crate** with
  `python3 {{SKILLS_ROOT}}/verus_check.py {{TARGET_PATH}} --project {{PROJECT_ROOT}} --whole-crate`:
  the pins live across many modules, so a per-module check
  on one file proves nothing.
- `END_REASON:COMPLETE` only when the whole crate verifies with zero admits.
"""
    if mode == "bridge-full" and target.stem == "ristretto":
        # The ristretto rung (--no-ristretto-proof) reuses the bridge-full
        # machinery (whole-crate verify + frozen-file guard + spec gate over
        # every allow-edit file) but the SCENARIO differs: NO lemmas are
        # deleted — only the RistrettoPoint::decompress proof layer (decompress
        # + its step_1/step_2 helpers) is stripped. The generic bridge-full text
        # below is hardwired to the edwards/montgomery decompress deletion
        # scenario, which would mislead a ristretto agent, so render a tailored
        # block here. (Edwards rungs are unaffected — they fall through.)
        # NOTE: gated on len(allow_edit)==1 (this branch is reached only when the
        # full-stack branch above did not fire, i.e. ristretto.rs is the sole
        # editable file).
        return f"""## EXPERIMENT MODE — read this first

This is the **ristretto** rung — pure proof reconstruction ONE LAYER UP from
edwards. The standard rules below mostly apply; this section adjusts them and
wins where they conflict.

**Editable file (the ONLY file you may touch):**
{bullets}

`RistrettoPoint` is a user-facing API built directly ON TOP of the Edwards
layer. This is a pure **proof-reconstruction** task: the proof bodies of
`CompressedRistretto::decompress` and its two dedicated proof helpers
`decompress::step_1` and `decompress::step_2` (in `ristretto.rs`) have been
**stripped** — their signatures, `requires`/`ensures` contracts, and executable
code are byte-identical to the original, but the inline `proof {{ ... }}` blocks
and proof-only `assert`s are gone, so `cargo verus` now fails those three
functions with *"postcondition not satisfied"*. **Rewrite those three proofs**
(and add whatever NEW helper `proof fn` lemmas you want, in `ristretto.rs`) so
the **whole crate verifies**.

**NOTHING has been deleted and NO spec definition is editable.** This is the key
difference from the other rungs: do NOT go looking for deleted lemmas or missing
spec functions — there are none. The ENTIRE substrate is frozen and correct:
- the whole edwards / montgomery / field / number-theory proof tree (incl. the
  decompress layer you may have seen in other rungs),
- the ristretto **spec vocabulary** `specs/ristretto_specs.rs`
  (`spec_ristretto_decompress`, `ristretto_decode_x/y/ok`, `is_ristretto_coset`,
  …) and `edwards_specs` (`is_well_formed_edwards_point`, `is_in_even_subgroup`,
  `edwards_point_as_nat`, …),
- the ristretto **lemma layer** `lemmas/ristretto_lemmas/*` — including the
  ristretto **axioms** (`axiom_ristretto_decode_on_curve`,
  `axiom_ristretto_decode_in_even_subgroup`), which your proofs will CALL but
  must not redefine.
You reconstruct ONLY the three stripped proofs (+ your own helper lemmas).

**Leave the rest of `ristretto.rs` alone.** The file also contains unrelated,
fully-proven APIs (`compress`, batch compress, `From`, `Eq`, `Neg`) with their
own `open spec fn`s (`batch_state_*`, `from_spec`, `eq_spec`, `neg_spec`) and
proofs. Do NOT touch any of them — their contracts are gate-frozen, their specs
are consumed by frozen callers, and their proofs already pass. You write proofs
ONLY inside `decompress` / `step_1` / `step_2` (and new helper `proof fn`s).

**What pins your reconstruction (why this is sound, and hard):**
- The `decompress`, `step_1`, and `step_2` **contracts** (signature +
  `requires`/`ensures`) are FROZEN by the spec-integrity gate (it snapshots
  every header in this editable file). A single edit to any of them fails the
  round as SPEC_DRIFT — even if Verus turns green.
- Those contracts are written ONLY in frozen spec vocabulary (the spec fns above,
  all in files you may NOT edit), so the user-facing meaning of decompress is
  fixed no matter what helper lemmas you add. A too-weak helper merely fails to
  discharge the frozen `ensures` (→ not COMPLETE), never a silent weakening.
- Every `open spec fn` elsewhere in `ristretto.rs` is pinned by its own frozen
  consumer + the whole-crate verify; do not edit them (see above).

**Rules (override the standard rules below):**
- **Edit ONLY `ristretto.rs`.** Every other file is FROZEN; editing any of them
  fails the round as FROZEN_EDIT and can never be COMPLETE, even if Verus turns
  green.
- Within `ristretto.rs` write ONLY proofs: the three stripped proof bodies and
  any NEW helper `proof fn` lemmas (each with a contract). You may NOT add or
  change any `spec fn`, nor edit any `fn` header / `requires` / `ensures` /
  `decreases`, nor touch the unrelated APIs.
- Do not modify any Rust *exec* code — exec bodies, signatures, types, imports
  are correct as written. Your edits add Verus annotations + proofs only.
- No `admit()`, `assume(...)`, `#[verifier::external_body]`, no new
  `proof fn axiom_*`.
- **Reconstruct from the code in front of you — do NOT recover the originals
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. Copying the pre-strip proofs back is retrieval, not
  reconstruction — it fails the round (GIT_RECOVERY) and is never COMPLETE.
- **Verify the WHOLE crate** with
  `python3 {{SKILLS_ROOT}}/verus_check.py {{TARGET_PATH}} --project {{PROJECT_ROOT}} --whole-crate`:
  a per-module check on `ristretto.rs` alone is a
  useful inner loop, but COMPLETE requires the whole crate green.
- `END_REASON:COMPLETE` only when the whole crate verifies with zero admits.
"""
    if mode == "bridge-full":
        return f"""## EXPERIMENT MODE — read this first

This is the bridge-full rung — the hardest. The standard rules below mostly
apply; this section adjusts them and wins where they conflict.

**Editable files (the ONLY files you may touch):**
{bullets}

This is a pure **proof-reconstruction** task. EVERY spec definition is frozen —
including the Montgomery↔Edwards map (`montgomery_to_edwards_affine`,
`edwards_y_from_montgomery_u`) and all the validity/curve predicates the API
contracts are written in. You rebuild only PROOFS so the **whole crate verifies**.

**A set of decompress-path lemmas was DELETED entirely** — signature, contract,
and body. The frozen proofs that call them (in `edwards.rs::decompress`,
`montgomery.rs::to_edwards`, and a few curve lemmas) now reference missing
functions, so the crate no longer compiles. **Re-create each deleted lemma** —
its signature (fixed by the callsites), a `requires`/`ensures` contract strong
enough for every caller, and a real proof — and add whatever NEW helper lemmas
you need. This includes pure curve facts you must re-derive from scratch, e.g.
"x = 0 on the curve ⟹ y² = 1" and "the parity fixes a unique x".

**Also rebuild any STRIPPED proof body in the editable files.** A function may
have its `requires`/`ensures`/signature intact but its proof emptied (it still
compiles its executable code, but Verus fails its postcondition). If one of the
editable files is a `.rs` whose own `fn` (e.g. an exec fn) has been left without
a proof, write that proof too — keeping its contract and signature exactly as
given. (If no such function is present, ignore this.)

**Leave every OTHER lemma alone.** The editable files also contain unrelated,
fully-proven lemmas (e.g. the Edwards group law — addition, scalar mult). Do not
touch them; their contracts are gate-frozen and their proofs already pass.

**What pins your reconstruction (why this is sound, and hard):**
- Every spec **definition** is frozen and lives in a file you may NOT edit (the
  map module, `edwards_specs`/`montgomery_specs`, `field_specs`). You cannot
  redefine the meaning of anything the contracts reference — you can only prove
  the obligations as stated.
- Each deleted lemma was called by some FROZEN proof or kept lemma, so your
  re-created version must have a compatible signature AND a contract strong
  enough for every caller. Too weak → the frozen caller fails to verify → the
  whole crate fails → not COMPLETE. You cannot quietly weaken anything: the
  user-facing contracts (`decompress`, `to_edwards`) are frozen and written only
  in frozen specs, so their meaning is fixed no matter what contracts you choose.
- The untouched group-law lemmas keep their gate-frozen contracts — do NOT edit,
  weaken, strengthen, or remove any of their `requires`/`ensures`/signatures
  (SPEC_DRIFT). Only real proofs make every frozen consumer verify at once; you
  cannot "assume" your way out.

**Rules (override the standard rules below):**
- **Edit ONLY the files listed above.** Every other file — `montgomery.rs`,
  `edwards.rs`, the map module, the other lemma files, the `specs/*` modules —
  is FROZEN. Editing any of them fails the round as FROZEN_EDIT and can never be
  COMPLETE, even if Verus turns green.
- Within the editable files you write ONLY proofs: the re-created deleted lemmas
  (each with a contract) and any NEW helper lemmas. You may NOT write or change
  any spec fn, nor touch the unrelated group-law lemmas (see the pins above).
- No `admit()`, `assume(...)`, `#[verifier::external_body]`, no new
  `proof fn axiom_*`.
- **Reconstruct from the code in front of you — do NOT recover the originals
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. Copying the pre-strip definitions/proofs back is
  retrieval, not reconstruction — it fails the round (GIT_RECOVERY) and is
  never COMPLETE.
- **Verify the WHOLE crate** with
  `python3 {{SKILLS_ROOT}}/verus_check.py {{TARGET_PATH}} --project {{PROJECT_ROOT}} --whole-crate`:
  the pins live in other modules, so a per-module
  check on one file alone proves nothing.
- `END_REASON:COMPLETE` only when the whole crate verifies with zero admits.
"""
    if mode == "bridge-specs":
        return f"""## EXPERIMENT MODE — read this first

This is the bridge-specs rung — the hardest. The standard rules below
mostly apply; this section adjusts them and wins where they conflict.

**Editable file (the ONLY file you may touch):**
{bullets}

Two shared `open spec fn`s — the Montgomery↔Edwards birational map — have
been **DELETED** from that file:
- `edwards_y_from_montgomery_u(u: nat) -> nat`
- `montgomery_to_edwards_affine(u: nat, sign_bit: u8) -> (nat, nat)`

Because they are gone, the crate no longer compiles: the re-exports in
`edwards_specs`/`montgomery_specs` fail with *"unresolved import"*, and the
callsites in `montgomery.rs` (`MontgomeryPoint::to_edwards`), `edwards.rs`
(`decompress`), and the edwards/curve helper lemmas fail with *"cannot find
function …"*.

**Your job:** reconstruct BOTH spec functions — signatures AND bodies — in
the editable file, so the **whole crate verifies** again. You are rebuilding
the *mathematical definitions*, not a proof.

**What pins your reconstruction (why this is sound, and hard):**
- The **signatures** are fixed by every callsite (the re-exports in
  `edwards_specs`/`montgomery_specs`, and the calls in `to_edwards`,
  `decompress`, and the lemmas). A wrong name/arity/type leaves the crate
  uncompilable.
- The **bodies** are pinned by FROZEN proofs you may NOT edit. In particular
  `montgomery_to_edwards_affine` appears in the **frozen postcondition of
  `MontgomeryPoint::to_edwards`**, whose proof (in `montgomery.rs`) is frozen
  and re-verified every round. A vacuous or wrong definition will fail
  `to_edwards` (or `decompress`, or a curve lemma) — it cannot be hidden.
- You therefore cannot "define your way out": only the honest map makes
  every frozen consumer verify simultaneously.

**Rules (override the standard rules below):**
- **Edit ONLY the file listed above.** Every other file — `montgomery.rs`,
  `edwards.rs`, the lemma files, the `specs/*` modules — is FROZEN. Editing
  any of them fails the round as FROZEN_EDIT and can never be COMPLETE, even
  if Verus turns green. (This is the cheat the guard exists to catch: you must
  not weaken `to_edwards`'s or `decompress`'s contract or proof to fit a
  convenient definition.)
- Do not modify any Rust *exec* code, types, or imports. You only write the
  two `pub open spec fn` definitions.
- No `admit()`, `assume(...)`, `#[verifier::external_body]`, no new
  `proof fn axiom_*`.
- **Reconstruct from the code in front of you — do NOT recover the originals
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. The history holds the pre-strip definitions; copying
  them back is retrieval, not reconstruction — it fails the round
  (GIT_RECOVERY) and is never COMPLETE.
- **Verify the WHOLE crate** with
  `python3 {{SKILLS_ROOT}}/verus_check.py {{TARGET_PATH}} --project {{PROJECT_ROOT}} --whole-crate`:
  the pin lives in other modules, so a
  per-module check on the bridge file alone proves nothing.
- `END_REASON:COMPLETE` only when the whole crate verifies with zero admits.
"""
    if mode == "proof-only":
        return f"""## EXPERIMENT MODE — read this first

This is constrained admit-filling. The standard rules below mostly
apply; this section adjusts them.

**Anchor:** `{target}` — read-only. Specs and proof body intact.

**Dependency files (you edit these — proof scaffolding only):** these
files compile but verus_check fails on them. Postconditions don't
follow from bodies, callsite preconditions aren't established, loops
have no decreases.

{bullets}

**All Rust code is correct as written, and all Verus specs (fn
headers, lemma signatures, `requires` / `ensures` / `decreases`) are
complete and fixed.** Your edits only add proof scaffolding inside fn
bodies — loop `invariant`s and loop-level `decreases`, `assert(...)`,
`assert(...) by (existing_lemma(...))`, `proof {{ ... }}` blocks, ghost
bindings.

**Your job:** add the proof scaffolding needed to close the existing
specs. Run `python3 {{SKILLS_ROOT}}/verus_check.py <absolute-dep-file.rs> --project {{PROJECT_ROOT}}`
on each dep file; the errors point at every
missing piece.

**Hard constraints — work within the existing skeleton:**
- **No new `proof fn` declarations.** No new helper lemmas, no new
  lemma skeletons (even with `admit()` body), no new axioms (no new
  `proof fn axiom_*`). The lemma library is exactly what's currently
  on disk — use it, don't extend it.
- **No fn-header changes anywhere.** Do not modify any `requires` /
  `ensures` / `decreases`, on any fn. Spec drift fails the round.
- **Do not modify any Rust code.** Exec bodies, exec fn signatures,
  types, and imports are correct as written.
- No `admit()`, no `assume(...)`, no `#[verifier::external_body]`.
- **Reconstruct from the code in front of you — do NOT recover the original
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. The worktree's history still holds the pre-strip proofs;
  copying them back is retrieval — it fails the round (GIT_RECOVERY).
- Edit only the dep files listed above.

**If a postcondition appears unprovable from the body within the
existing lemma library, emit `END_REASON:LIMIT`** — do not relax the
spec, do not add an axiom, do not invent a helper.

**Verification:** run `python3 {{SKILLS_ROOT}}/verus_check.py <absolute-file.rs> --project {{PROJECT_ROOT}}`
on the anchor AND on each dep file separately. `END_REASON:COMPLETE` only when
every check passes.
"""
    # default: spec-proof
    return f"""## EXPERIMENT MODE — read this first

This is a spec-reconstruction experiment, not the usual admit-filling
workflow. Read this section before the standard rules below; where they
conflict, this section wins.

**Anchor:** `{target}` — read-only. It has the top-level pub fn's
`requires` / `ensures` (the user-visible API contract). This and the
standard library specs are the only Verus specs you may treat as given.

**Dependency files (you edit these — Verus annotations only):**
intermediate functions in the call chain below the anchor have no
Verus specs — bare Rust fn headers, with bodies that compile but do
not verify.

{bullets}

**All Rust code in these files is correct as written.** Do not modify
any exec body, exec fn signature, type definition, or `use` import.
Your edits only add or modify Verus annotations: `requires` / `ensures`
/ `decreases` on fn headers, loop `invariant`s and loop-level
`decreases`, `assert(...)`, `assert(...) by (...)`, `proof {{ ... }}`
blocks, ghost bindings, and helper `proof fn lemma_*` declarations.

**Your job:** given (i) the anchor's contract, (ii) the Rust bodies in
front of you, and (iii) the standard library spec vocabulary, infer
`requires` / `ensures` / `decreases` on each intermediate function —
strong enough that the anchor's proof discharges, weak enough that the
function body proves them. Add the proof scaffolding needed to close
those specs.

**You have full creative freedom over proof structure.** All Verus
proof forms are permitted: helper lemmas, broadcast use, assert-by,
reveal_with_fuel, ghost variables, custom lemma libraries.
**Helper-lemma refactoring for maximum reuse is strongly encouraged.**
If multiple obligations share an algebraic identity, lift it into a
reusable lemma and reuse.

**Rules (override the standard rules below):**
- Edit only the dependency files listed above. The anchor and unrelated
  siblings stay untouched.
- Do not modify any Rust code in the dep files — exec bodies, exec fn
  signatures, types, and imports are correct as written.
- No `admit()`, `assume(...)`, or `#[verifier::external_body]`.
- **Reconstruct from the code in front of you — do NOT recover the original
  from version control.** No `git show`/`restore`/`checkout`/`log -p`/`diff`,
  no reading `.git/`. The worktree's history still holds the pre-strip specs
  and proofs; copying them back is retrieval, not reconstruction — it fails
  the round (GIT_RECOVERY) and is never COMPLETE.
- The in-loop spec-integrity gate is disabled for this run. Post-hoc
  analysis compares your specs to alternative phrasings.
- **Run `python3 {{SKILLS_ROOT}}/verus_check.py <absolute-file.rs> --project {{PROJECT_ROOT}}`
  on the anchor AND on each dependency file separately** — `--verify-module` is module-scoped, so checking only
  the anchor won't surface errors inside a dep fn's proof body. All
  checks must return `okay: true` before declaring done.
- `END_REASON:COMPLETE` only when every check passes.
"""


def run_subskill(cmd: list[str], env: dict, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run a skill CLI; capture stdout/stderr as strings."""
    proc = subprocess.run(
        cmd, env=env, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ----------------- the round -----------------

# Module-level handle to the live claude subprocess so a SIGTERM to run.py
# can propagate the kill to the whole process group. Without this, killing
# run.py orphans claude and any subprocesses it spawned (cargo verus, z3,
# Monitor poll loops, ...).
_LIVE_PROC: Optional[subprocess.Popen] = None
_RECEIVED_SIGNAL: Optional[int] = None

# Module-level handle to the optional --wire-log proxy (wire_proxy.py). Tracked
# so it is killed on signal and at interpreter exit — otherwise its serve_forever
# loop would outlive run.py as an orphan, exactly like the claude tree above.
_WIRE_PROC: Optional[subprocess.Popen] = None


def _install_signal_handler() -> None:
    import signal as _signal
    import os

    def _handler(signum, _frame):
        global _LIVE_PROC, _RECEIVED_SIGNAL
        _RECEIVED_SIGNAL = signum
        proc = _LIVE_PROC
        if proc is not None and proc.poll() is None:
            print(f"\n[run] received signal {signum} — killing claude process group {proc.pid}",
                  flush=True)
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass
            _kill_wire_proxy()
            # Let run_claude_round return so the no-result path can persist a
            # tainted round_N.json/result.json before main exits 128+signum.
            return
        _kill_wire_proxy()
        # No live Claude round exists to persist; preserve signal-like exit.
        raise SystemExit(128 + signum)

    for sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
        _signal.signal(sig, _handler)


def _start_wire_proxy(claude_raw_dir: Path, env: dict) -> None:
    """Route the claude subprocess through a localhost logging proxy via
    ANTHROPIC_BASE_URL — the only API-capture method that works with the
    native-binary claude (claude-trace's JS-patching approach is dead on v2.x).

    Best-effort: on ANY failure we warn and leave `env` untouched, so the run
    proceeds normally straight to api.anthropic.com — wire logging must never
    fail a proof round. The proxy writes claude_raw/wire_{prefixes,requests}.jsonl
    (full system prompt + tool schemas once, then per-turn message deltas).
    """
    global _WIRE_PROC
    import atexit
    import socket
    import time
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        claude_raw_dir.mkdir(parents=True, exist_ok=True)
        log = open(claude_raw_dir / "wire_proxy.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "wire_proxy.py"), str(port),
             str(claude_raw_dir)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        for _ in range(100):                      # wait for the listener to bind
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("listener did not bind within ~5s")
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        _WIRE_PROC = proc
        atexit.register(_kill_wire_proxy)
        print(f"[run] --wire-log: proxy on 127.0.0.1:{port} -> "
              f"{claude_raw_dir}/wire_*.jsonl", flush=True)
    except Exception as e:
        print(f"[run] --wire-log: could not start proxy ({e}); continuing "
              f"without wire logging", flush=True)


def _kill_wire_proxy() -> None:
    global _WIRE_PROC
    proc = _WIRE_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    _WIRE_PROC = None


def run_claude_round(
    prompt: str,
    cwd: Path,
    env: dict,
    raw_out: Path,
    session_id: str,
    resume: bool,
    model: Optional[str] = None,
    deadline_seconds: Optional[float] = None,
    continue_message: Optional[str] = None,
) -> tuple[Optional[str], int, dict]:
    """Invoke `claude -p` (fresh session pinned to `session_id`) or
    `claude --resume <session_id> -p` (continue THAT specific session).
    Stream NDJSON to `raw_out`. Parse END_REASON from the final
    `type:"result"` line. Return (end_reason, returncode, claude_result_dict).

    The session is identified explicitly by UUID rather than via `-c`'s
    "most recent session in this directory" lookup. `-c` is mtime-based
    and globally scoped to the OAuth user, so a concurrent interactive
    Claude Code session in the same project dir would always win the
    tiebreaker and quietly hijack the harness's continuation rounds.
    See: investigation of curve_eq_20260518 — 6 of 10 rounds were
    re-routed to the user's interactive session because of that.

    `deadline_seconds`: if set, SIGKILL the entire process group when the
    deadline expires. Catches the case where the agent spawns background
    subprocesses (Monitor + sleep loops, async cargo verus + pkill chains)
    that hold the claude -p process alive forever.

    `start_new_session=True`: puts claude (+ all its descendants) in a
    fresh process group so we can kill the whole tree at once.
    """
    import os, signal as _signal
    # Install the PreToolUse verifier-policy hook for this round (both fresh and
    # resume invocations) so forbidden verifier Bash patterns are blocked at the
    # tool call, not just caught post-round.
    settings_path = _write_agent_settings(raw_out.parent)
    settings_flags = ["--settings", str(settings_path)]
    if resume:
        cmd = ["claude", "--resume", session_id, "-p",
               "--verbose", "--output-format", "stream-json",
               "--permission-mode", "bypassPermissions",
               *AGENT_TOOL_FLAGS, *settings_flags]
        if model:
            cmd += ["--model", model]
        # The trailing arg becomes the next user message. Default to a
        # bare "continue"; callers may pass a richer message (e.g.
        # structured round-history feedback) to nudge the agent.
        cmd += [continue_message or "continue"]
    else:
        cmd = ["claude", "-p", "--session-id", session_id,
               "--verbose", "--output-format", "stream-json",
               "--permission-mode", "bypassPermissions",
               *AGENT_TOOL_FLAGS, *settings_flags]
        if model:
            cmd += ["--model", model]
        cmd += [prompt]

    raw_out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] claude subprocess → {raw_out}", flush=True)
    global _LIVE_PROC
    with open(raw_out, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, env=env, cwd=str(cwd),
            start_new_session=True,
        )
        _LIVE_PROC = proc
        # Enforce the deadline against the WALL clock (time.time), polling in
        # short slices. proc.wait(timeout=...) alone counts down against
        # time.monotonic(), which freezes during macOS sleep — once let a
        # round run 7.8h past a 90-min budget on a sleeping laptop.
        wall_deadline = (time.time() + deadline_seconds) if deadline_seconds else None
        while True:
            try:
                proc.wait(timeout=(30 if wall_deadline else None))
                break
            except subprocess.TimeoutExpired:
                if wall_deadline and time.time() >= wall_deadline:
                    print(f"[run] deadline ({deadline_seconds:.0f}s) exceeded — "
                          f"killing claude process group {proc.pid}", flush=True)
                    try:
                        os.killpg(proc.pid, _signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    break
        # Post-completion cleanup: claude may have left background bash
        # children (Monitor poll loops, etc.). If anything is still alive
        # after the main process returned, kill the whole group.
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        _LIVE_PROC = None

    # Parse the final result line.
    last = ""
    try:
        with open(raw_out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line.strip()
    except OSError:
        pass

    result_dict: dict = {}
    end_reason: Optional[str] = None
    if last:
        try:
            parsed = json.loads(last)
            if parsed.get("type") == "result":
                result_dict = parsed
                text = parsed.get("result", "")
                m = END_REASON_RE.search(text)
                if m:
                    end_reason = m.group(1).upper()
        except json.JSONDecodeError:
            pass

    return end_reason, proc.returncode, result_dict


def _last_raw_json_event(raw_out: Path) -> dict:
    last = ""
    try:
        with open(raw_out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line.strip()
    except OSError:
        return {}
    if not last:
        return {}
    try:
        parsed = json.loads(last)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _claude_auto_memory_dir(raw_out: Path) -> Optional[Path]:
    """Return Claude Code's auto-memory directory from a stream-json init event."""
    try:
        with open(raw_out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                memory_paths = event.get("memory_paths")
                if isinstance(memory_paths, dict) and memory_paths.get("auto"):
                    return Path(str(memory_paths["auto"]))
    except OSError:
        return None
    return None


def _snapshot_claude_memory(raw_out: Path, tdir: Path, round_num: int) -> Optional[Path]:
    """Copy Claude Code auto-memory into results for audit and reset carryover."""
    src = _claude_auto_memory_dir(raw_out)
    if src is None or not src.is_dir():
        return None
    root = tdir / "claude_memory"
    dest = root / f"round_{round_num}"
    latest = root / "latest"
    try:
        root.mkdir(parents=True, exist_ok=True)
        for path in (dest, latest):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            shutil.copytree(src, path)
    except OSError as e:
        print(f"[run] WARNING: could not snapshot Claude memory {src}: {e}",
              flush=True)
        return None
    return dest


def _render_claude_memory_carryover(
    tdir: Path, max_files: int = 6, max_chars: int = 6000
) -> str:
    """Render the latest captured Claude auto-memory as a bounded prompt block."""
    latest = tdir / "claude_memory" / "latest"
    if not latest.is_dir():
        return ""
    chunks: list[str] = []
    remaining = max_chars
    files = sorted(
        [p for p in latest.rglob("*") if p.is_file()],
        key=lambda p: (
            p.suffix.lower() not in (".md", ".txt"),
            str(p.relative_to(latest)),
        ),
    )
    for path in files[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text or remaining <= 0:
            continue
        rel = path.relative_to(latest)
        header = f"## {rel}\n"
        budget = max(0, remaining - len(header) - 32)
        if budget <= 0:
            break
        clipped = text[:budget]
        if len(text) > budget:
            clipped = clipped.rstrip() + "\n[truncated]"
        chunk = header + clipped
        chunks.append(chunk)
        remaining -= len(chunk)
    if not chunks:
        return ""
    return (
        "Claude project memory carried over from the previous fresh-session "
        "scratch cwd. These are your own reconstruction notes from this run; "
        "use them as context, but the current files and verifier output remain "
        "authoritative.\n\n" + "\n\n".join(chunks)
    )


def _fresh_session_prompt(
    base_prompt: str, continue_message: Optional[str], memory_carryover: str
) -> str:
    """Append reset carryover that would otherwise be lost without --resume."""
    parts: list[str] = []
    if continue_message and continue_message != "continue":
        parts.append("Harness carryover from the previous session:\n\n" + continue_message)
    if memory_carryover:
        parts.append(memory_carryover)
    if not parts:
        return base_prompt
    return base_prompt + "\n\n# Fresh-session carryover\n\n" + "\n\n".join(parts)


def _raw_mentions_user_interrupt(raw_out: Path) -> bool:
    """Whether Claude's stream explicitly says the user interrupted the run."""
    needles = ("[Request interrupted by user]",
               "[Request interrupted by user for tool use]")
    try:
        with open(raw_out, "r", encoding="utf-8", errors="replace") as f:
            return any(any(needle in line for needle in needles) for line in f)
    except OSError:
        return False


def _classify_claude_no_result_exit(
    rc: int, claude_result: dict, raw_out: Path,
    interrupted_signal: Optional[int] = None,
) -> Optional[dict]:
    """Classify a nonzero Claude exit that produced no final result event."""
    if claude_result:
        return None
    last_event = _last_raw_json_event(raw_out)
    if _raw_mentions_user_interrupt(raw_out):
        return {
            "reason": "USER_INTERRUPTED",
            "event": last_event,
            "message": (
                f"claude exited rc={rc} without a result event after user "
                f"interruption"
            ),
        }
    sig = _RECEIVED_SIGNAL if interrupted_signal is None else interrupted_signal
    if sig is not None:
        return {
            "reason": "INTERRUPTED_SIGNAL",
            "event": last_event,
            "signal": sig,
            "message": (
                f"run.py received signal {sig}; claude exited rc={rc} without "
                f"a result event after harness interruption"
            ),
        }
    if rc <= 0:
        return None
    if (last_event.get("type") == "system"
            and last_event.get("subtype") == "api_retry"):
        attempt = last_event.get("attempt")
        max_retries = last_event.get("max_retries")
        status = last_event.get("error_status")
        err = last_event.get("error")
        return {
            "reason": "RETRY_EXHAUSTED",
            "event": last_event,
            "message": (
                f"claude exited rc={rc} without a result event after API "
                f"retry {attempt}/{max_retries} "
                f"(status={status!r}, error={err!r})"
            ),
        }
    return {
        "reason": "TRANSPORT_ERROR",
        "event": last_event,
        "message": f"claude exited rc={rc} without a result event",
    }


def _is_generated_editable_contract_claim(
    file_path: Path, function: str, allow_edit: Optional[list[Path]]
) -> bool:
    """False-contract escalation is only for frozen contracts.

    Generated lemma contracts in editable lemma files are repairable by design;
    if they are weak or false, the agent should strengthen the contract rather
    than terminate the run as FALSE_CONTRACT.
    """
    if not function.startswith("lemma_"):
        return False
    try:
        resolved = file_path.resolve()
    except OSError:
        return False
    if "/lemmas/" not in resolved.as_posix():
        return False
    generated_files = {
        str(p.resolve()) for p in (allow_edit or [])
        if "/lemmas/" in p.resolve().as_posix()
    }
    return str(resolved) in generated_files


def _verify_false_contract_claims(
    tdir: Path, project: Path, snapshot: Path,
    allow_edit: Optional[list[Path]] = None
) -> tuple[list[dict], list[dict]]:
    """E7: the agent escalated FALSE_CONTRACT and (should have) written its
    counterexample claims to `<tdir>/false_contract_claims.json`
    (a list of {function, file, witness}). Re-verify EACH against the
    frozen snapshot via skills/check_false_contract.py — do not trust the
    agent's say-so. Returns (verified, unconfirmed)."""
    claims_path = tdir / "false_contract_claims.json"
    if not claims_path.exists():
        return [], []
    try:
        claims = json.loads(claims_path.read_text())
        assert isinstance(claims, list)
    except (OSError, json.JSONDecodeError, AssertionError):
        return [], []
    checker = Path(__file__).resolve().parent / "skills" / "check_false_contract.py"
    marker_prefix = hashlib.sha1(tdir.parent.name.encode()).hexdigest()[:8]
    verified, unconfirmed = [], []
    for c in claims:
        if not isinstance(c, dict) or "function" not in c or "file" not in c:
            continue
        # Resolve the claim file against the project worktree if it isn't already
        # absolute (the agent may write `src/lemmas/...`). The checker re-anchors
        # to the snapshot's canonical file, but pass a path that actually exists.
        fpath = Path(c["file"])
        if not fpath.is_absolute():
            for cand in (project / fpath, project / "src" / fpath, project / ".." / fpath):
                if cand.exists():
                    fpath = cand.resolve(); break
        res = {}
        if _is_generated_editable_contract_claim(
                fpath, str(c["function"]), allow_edit):
            res = {
                "reason": (
                    "claim targets an editable generated lemma contract; "
                    "repair the contract instead of escalating FALSE_CONTRACT"),
                "failure_class": "editable_generated_contract",
            }
        else:
            cmd = [sys.executable, str(checker), "--snapshot", str(snapshot),
                   "--project", str(project), "--file", str(fpath),
                   "--function", str(c["function"]),
                   "--witness", json.dumps(c.get("witness", {})),
                   "--marker-prefix", marker_prefix]
            try:
                # The checker self-bounds cargo verus (--timeout); this outer cap is
                # a backstop so a wedged checker can't hang the round.
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                res = json.loads(out.stdout or "{}")
            except subprocess.TimeoutExpired:
                res = {"reason": "checker timed out", "failure_class": "checker_timeout"}
            except (OSError, json.JSONDecodeError):
                res = {"reason": "checker invocation/parse failed"}
        entry = {"function": c["function"], "file": str(fpath),
                 "witness": c.get("witness", {}),
                 "reason": res.get("reason", ""),
                 "failure_class": res.get("failure_class", ""),
                 "returncode": res.get("returncode"),
                 "tripwire_fired": res.get("tripwire_fired"),
                 "witness_failed": res.get("witness_failed"),
                 "compile_failed": res.get("compile_failed"),
                 "module_not_clean": res.get("module_not_clean"),
                 "foreign_error_lines": res.get("foreign_error_lines"),
                 "stderr_excerpt": res.get("stderr_excerpt", "")[-600:]}
        (verified if res.get("verified") else unconfirmed).append(entry)
    return verified, unconfirmed


def _finalize_false_contract_round(
    tdir: Path, rr: RoundResult, verified_false: list[dict]
) -> str:
    """Persist the machine-verified terminal label for a FALSE_CONTRACT claim."""
    final_reason = "FALSE_CONTRACT" if verified_false else "NEEDS_DECOMP"
    rr.end_reason = final_reason
    write_json(tdir / f"round_{rr.round_number}.json", rr)
    return final_reason


_GENERATED_CONTRACT_FIELDS = {"requires", "ensures", "decreases"}


def _partition_generated_contract_drift(
    drift: list[dict], allow_edit: Optional[list[Path]]
) -> tuple[list[dict], list[dict]]:
    """Generated lemma contracts are editable; original/API specs are not."""
    generated_files = {
        str(p.resolve()) for p in (allow_edit or [])
        if "/lemmas/" in p.as_posix()
    }
    allowed, blocked = [], []
    for d in drift:
        try:
            file_s = str(Path(d.get("file", "")).resolve())
        except OSError:
            file_s = ""
        if (file_s in generated_files
                and str(d.get("function", "")).startswith("lemma_")
                and d.get("field") in _GENERATED_CONTRACT_FIELDS):
            allowed.append(d)
        else:
            blocked.append(d)
    return allowed, blocked


def _build_spec_drift_diagnostic(
    drift: list[dict], allow_edit: Optional[list[Path]], *,
    returncode: int = 0, parse_error: str = "", stderr_tail: str = "",
) -> dict:
    allowed, blocking = _partition_generated_contract_drift(drift, allow_edit)
    out = {
        "returncode": returncode,
        "raw_drift": drift,
        "allowed_generated_contract_drift": allowed,
        "blocking_drift": blocking,
    }
    if parse_error:
        out["parse_error"] = parse_error
    if stderr_tail:
        out["stderr_tail"] = stderr_tail[-2000:]
    return out


def _run_spec_drift_diagnostic(
    target: Path, spec_snapshot: Path, env: dict,
    allow_edit: Optional[list[Path]],
) -> dict:
    rc, stdout, stderr = run_subskill(
        [sys.executable, str(HERE / "skills" / "spec_check.py"),
         "verify", str(target), "--against", str(spec_snapshot),
         "--check-spec-defs"],
        env=env,
    )
    try:
        drift = json.loads(stdout).get("drift", [])
    except json.JSONDecodeError as e:
        return _build_spec_drift_diagnostic(
            [], allow_edit, returncode=rc, parse_error=str(e),
            stderr_tail=stderr)
    return _build_spec_drift_diagnostic(
        drift, allow_edit, returncode=rc, stderr_tail=stderr)


def _restore_spec_drift_from_baseline(
    spec_drift: list[dict],
    snapshot_targets: list[Path],
    snapshots_root: Path,
    target: Path,
    spec_snapshot: Path,
    env: dict,
    allow_edit: Optional[list[Path]],
) -> dict:
    """Restore drifted frozen specs from the round-0 baseline and verify clean."""
    baseline_dir = snapshots_root / "round_0"
    by_file: dict[Path, list[dict]] = {}
    unresolved: list[str] = []
    restored_files: list[str] = []
    full_file_restored: list[str] = []

    snapshot_lookup: dict[str, Path] = {}
    for p in snapshot_targets:
        snapshot_lookup[str(p)] = p
        try:
            snapshot_lookup[str(p.resolve())] = p
        except OSError:
            pass

    for d in spec_drift:
        raw = str(d.get("file") or "")
        path = snapshot_lookup.get(raw)
        if path is None:
            try:
                path = snapshot_lookup.get(str(Path(raw).resolve()), Path(raw))
            except OSError:
                path = Path(raw)
        by_file.setdefault(path, []).append(d)

    for path, drift_items in by_file.items():
        snap = baseline_dir / _snapshot_name(path, snapshot_targets)
        label = str(path)
        if not path.exists() or not snap.exists():
            unresolved.append(label)
            continue
        drift_file = snapshots_root / "spec_drift_restore_input.json"
        drift_file.write_text(json.dumps({"drift": drift_items}, indent=2))
        rc_restore, restore_stdout, restore_stderr = run_subskill(
            [sys.executable, str(HERE / "skills" / "spec_check.py"),
             "restore", str(path), "--baseline", str(snap),
             "--drift-file", str(drift_file)],
            env=env,
        )
        try:
            restore_result = json.loads(restore_stdout)
        except json.JSONDecodeError:
            restore_result = {
                "okay": False,
                "unresolved": [f"{label}: bad restore output"],
                "stderr_tail": restore_stderr[-1000:],
            }
        if rc_restore != 0 or not restore_result.get("okay"):
            shutil.copy2(snap, path)
            restored_files.append(label)
            full_file_restored.append(label)
            continue
        if restore_result.get("changed"):
            restored_files.append(label)

    if unresolved:
        return {
            "okay": False,
            "restored_files": restored_files,
            "full_file_restored": full_file_restored,
            "unresolved": unresolved,
            "residual_drift": spec_drift,
        }

    diagnostic = _run_spec_drift_diagnostic(
        target, spec_snapshot, env, allow_edit)
    residual = diagnostic.get("blocking_drift", [])
    return {
        "okay": not residual,
        "restored_files": restored_files,
        "full_file_restored": full_file_restored,
        "unresolved": [],
        "residual_drift": residual,
        "diagnostic": diagnostic,
    }


def _spec_drift_verus_result(spec_drift: list[dict]) -> dict:
    """Synthetic verifier result for a round already failed by spec drift.

    A blocking spec drift makes the pre-restore state non-promotable, so running
    Verus before restoring only burns budget on a tainted state. Keep the round
    JSON diagnostic-shaped without pretending a verifier pass ran.
    """
    messages = []
    for d in spec_drift:
        function = d.get("function") or "<unknown>"
        field = d.get("field") or "<unknown>"
        original = d.get("original")
        current = d.get("current")
        detail = f"SPEC_DRIFT: {function} modified frozen {field}"
        if original is not None or current is not None:
            detail += f" (original={original!r}, current={current!r})"
        messages.append({
            "file": d.get("file", ""),
            "line": d.get("line", 0) or 0,
            "column": 0,
            "severity": "error",
            "data": detail,
        })
    if not messages:
        messages.append({
            "file": "",
            "line": 0,
            "column": 0,
            "severity": "error",
            "data": "SPEC_DRIFT: blocking spec drift detected",
        })
    return {
        "okay": False,
        "messages": messages,
        "error_count": len(messages),
        "skipped_verus_due_to_spec_drift": True,
    }


def _continue_work_tail(experiment_mode: str, admits_left: int) -> str:
    """One-line continuation nudge appended after round history."""
    if experiment_mode in _WHOLE_CRATE_MODES and admits_left == 0:
        return "\nContinue working on remaining whole-crate verification errors."
    return "\nContinue working on remaining admits."


def _plateau_directive_text(
    rounds_since_new_low: int, best_admits: Optional[int], experiment_mode: str
) -> str:
    """Fresh-session nudge after a duration-independent progress plateau."""
    if experiment_mode in _WHOLE_CRATE_MODES and best_admits == 0:
        return (
            f"You have made NO net progress on whole-crate verification for "
            f"{rounds_since_new_low} rounds after reducing hard admits to 0. "
            f"STOP using target-local completion/scope reasoning. This is a "
            f"whole-crate experiment: work the remaining Verus errors in the "
            f"editable files, including stripped API proof bodies. Top-level "
            f"API files in the editable list are in scope; only their contracts "
            f"are frozen.")
    return (
        f"You have made NO net progress on the remaining hard admits for "
        f"{rounds_since_new_low} rounds. STOP re-editing files that "
        f"already verify. The unproven obligations are in the hard-admit "
        f"inventory below — open the HIGHEST-COUNT file and discharge its "
        f"admits one at a time. If the anchor module shows 0 admits, do "
        f"not work it.")


# ----------------- the task -----------------

def run_task(
    target: Path,
    project: Path,
    run_id: str,
    results_root: Path,
    max_rounds: int,
    model: Optional[str] = None,
    vstd_root: Optional[Path] = None,
    admitted_ref: Optional[str] = None,
    truth_ref: str = "main",
    max_task_minutes: float = 45.0,
    skip_failure_memory: bool = False,
    verus_rlimit: Optional[float] = _DEFAULT_VERUS_RLIMIT,
    auto_reset: bool = True,
    max_auto_resets: int = 3,
    max_git_recovery_resets: int = 3,
    max_frozen_edit_resets: int = 3,
    stall_max_duration_sec: float = 180.0,
    bloat_threshold_tokens: int = 200_000,
    plateau_stop_rounds: int = 6,
    experiment_allow_edit: Optional[list[Path]] = None,
    experiment_active_edit: Optional[list[Path]] = None,
    experiment_mode: str = "spec-proof",
    no_spec_gate: bool = False,
    wire_log: bool = False,
    sibling_verify: bool = True,
) -> TaskResult:
    target = target.resolve()
    project = project.resolve()
    experiment_allow_edit = [p.resolve() for p in (experiment_allow_edit or [])]
    experiment_active_edit = [p.resolve() for p in (experiment_active_edit or [])]
    experiment_edit_scope = experiment_active_edit or experiment_allow_edit
    module = module_path_of(target, project)
    target_id = results.target_id_from_path(target)
    tdir = task_dir(results_root, run_id, target_id)
    catalog_cache = results_root / "catalog_cache.json"
    experiment_provenance = (
        os.environ.get("DALEK_EXPERIMENT_PROVENANCE", "").strip() or None
    )

    omitted_active_admits = _active_edit_omitted_admit_files(
        target, experiment_allow_edit, experiment_active_edit)
    if omitted_active_admits:
        names = ", ".join(str(p) for p in omitted_active_admits[:8])
        if len(omitted_active_admits) > 8:
            names += ", ..."
        msg = (
            "--experiment-active-edit freezes file(s) that still contain "
            f"non-axiom admits counted by COMPLETE: {names}. Include these "
            "files in --experiment-active-edit or discharge/admit-reset them "
            "before narrowing the active edit scope."
        )
        print(f"[error] {msg}", file=sys.stderr)
        task_result = TaskResult(
            task_id=target_id,
            run_id=run_id,
            target_path=str(target),
            module_path=module,
            success=False,
            end_reason="ERROR",
            rounds_used=0,
            duration_seconds=0.0,
            error_message=msg,
            experiment_provenance=experiment_provenance,
        )
        write_json(tdir / "result.json", task_result)
        return task_result

    # Scratch cwd for the claude subprocess, built once and reused for every
    # round of this task (stable cwd → stable session-project slug for
    # --resume). Keeps HERE's CLAUDE.md out of the agent's context. See
    # _make_agent_cwd for why it's a fresh per-task dir, not a shared global.
    agent_cwd = _make_agent_cwd(target_id)

    # Per-task isolated CLI log
    env = os.environ.copy()
    env["CLI_LOG_PATH"] = str(tdir / "cli.log")
    bash_env = _write_agent_bash_env(tdir, project)
    env["BASH_ENV"] = str(bash_env)
    env["DALEK_AGENT_PROJECT_ROOT"] = str(project)
    print(f"[run] Claude Bash tool startup cwd -> {project} (BASH_ENV={bash_env})",
          flush=True)

    # When running under Claude Code (CLAUDECODE=1), strip ANTHROPIC_API_KEY
    # so the spawned `claude -p` subprocess falls back to the user's logged-in
    # session auth. Otherwise an inherited (possibly stale) env-var key gets
    # rejected and every round fails with "Invalid API key" instantly.
    if env.get("CLAUDECODE") == "1":
        env.pop("ANTHROPIC_API_KEY", None)

    # --wire-log: capture the full API request bodies (system prompt + tool
    # schemas + skills + per-turn context growth) that stream-json never sees,
    # by routing claude through a localhost logging proxy. Subagents inherit
    # ANTHROPIC_BASE_URL via env, so their traffic is captured too. Best-effort.
    if wire_log:
        _start_wire_proxy(tdir / "claude_raw", env)

    # Discover sibling helpers in scope (rule 4 relaxation: the agent may
    # append new lemmas to siblings under lemmas/<area>_lemmas/). Empty
    # for tasks whose target has no recognized helper area.
    siblings: list[Path] = []
    try:
        rc_sib, sib_stdout, _ = run_subskill(
            [sys.executable, str(HERE / "skills" / "spec_check.py"),
             "list-siblings", str(target), "--project", str(project)],
            env=env,
        )
        if rc_sib == 0:
            siblings = [Path(p) for p in json.loads(sib_stdout).get("siblings", [])]
    except (json.JSONDecodeError, OSError):
        siblings = []

    if siblings:
        print(f"[run] siblings in scope ({len(siblings)}):", flush=True)
        for s in siblings:
            print(f"[run]   {s}", flush=True)

    # Snapshot specs (baseline for integrity gate) — covers target + siblings +
    # every allow-edit file. Including the allow-edit set matters when an
    # experiment makes a file editable that is NOT a sibling of the target (e.g.
    # montgomery.rs holding to_edwards): the frozen-file guard no longer protects
    # it, so the spec gate is the only thing freezing its contracts. After the
    # strip, an editable file's surviving fn headers ARE the frozen contracts
    # (deleted lemmas are gone; re-added ones are tolerated as additions).
    spec_snapshot = tdir / "spec_snapshot.json"
    # Agent-facing spec_check invocations should still pass --against explicitly,
    # but expose the per-task snapshot path so a common omitted-flag invocation
    # remains tied to this run's authoritative baseline.
    env["SPEC_SNAPSHOT"] = str(spec_snapshot)
    _snap_extra = [str(s) for s in siblings]
    for _p in (experiment_allow_edit or []):
        if str(_p) != str(target) and str(_p) not in _snap_extra:
            _snap_extra.append(str(_p))
    snap_cmd = [sys.executable, str(HERE / "skills" / "spec_check.py"),
                "snapshot", str(target), "--out", str(spec_snapshot)]
    if _snap_extra:
        snap_cmd += ["--siblings"] + _snap_extra
    rc, _, _ = run_subskill(snap_cmd, env=env)
    if rc != 0:
        return TaskResult(
            task_id=target_id, run_id=run_id, target_path=str(target),
            module_path=module, success=False, end_reason="ERROR",
            rounds_used=0, duration_seconds=0.0,
            error_message="spec_check snapshot failed",
            experiment_provenance=experiment_provenance,
        )

    # Pull prior failures → prompt block. Skippable for runs where prior
    # records predate prompt/harness improvements and would prime the
    # agent to give up.
    if skip_failure_memory:
        prior = []
        print("[run] failure_memory: SKIPPED (--no-failure-memory)", flush=True)
    else:
        prior = failure_memory.query(results_root, module, target_id)
    failure_block = failure_memory.as_prompt_block(prior)

    # Feature 3: prepend the cross-round discovery brief (files/searches a
    # prior attempt on this target already explored) so the retry doesn't
    # re-walk the tree. Injected regardless of failure-memory skip.
    brief_block = discovery_brief.load_block(results_root, target_id)
    if brief_block:
        print("[run] Feature3: injecting prior discovery brief", flush=True)
        failure_block = (
            "### Prior exploration map (discovery brief)\n\n"
            + brief_block + "\n\n" + failure_block
        )

    # Feature2 — escalation retry. If a prior attempt on this target declared
    # END_REASON:NEEDS_DECOMP ("this proof needs missing infrastructure"), give
    # the retry more room and a directive to build that infrastructure FIRST.
    # The escalation is "surprisingly informative" (AutoformBot): it tells us
    # the bottleneck is a missing lemma/chain, not merely a hard-but-tractable
    # proof, so widening the budget and front-loading the build is the right
    # response. NOTE: this mutates max_rounds / max_task_minutes BEFORE the
    # round loop reads them (first reads at the `for round_num` loop and the
    # remaining-budget calc, both well below here), so the bump takes effect.
    # Prepended AFTER the discovery brief so the build-first directive lands at
    # the very top, then the exploration map, then the raw failure records.
    prior_decomp = [r for r in prior
                    if (r.end_reason or "").upper() == "NEEDS_DECOMP"]
    if prior_decomp:
        max_rounds += 2
        max_task_minutes *= 1.5
        directive = (
            "## Escalation follow-up — prior attempt declared NEEDS_DECOMP\n\n"
            "A prior attempt escalated this target as needing **missing "
            "infrastructure** (a helper lemma / lemma-chain that does not "
            "exist, or a sub-lemma split). **Build that infrastructure FIRST**: "
            "define the missing helper lemma(s) in the target or a sibling "
            "`lemmas/<area>_lemmas/*.rs` file, verify them in isolation, THEN "
            "use them to discharge the admit(s). Read the prior error(s) below "
            "for what was reported missing. Do NOT re-escalate without having "
            "attempted to build the named infrastructure.\n"
        )
        failure_block = directive + ("\n" + failure_block if failure_block else "")
        print(f"[run] Feature2: prior NEEDS_DECOMP on {target_id} "
              f"({len(prior_decomp)} record(s)) — retry budget bumped to "
              f"rounds={max_rounds}, max_task_minutes={max_task_minutes:.0f} "
              f"+ build-infrastructure-first directive prepended", flush=True)

    experiment_block = build_experiment_block(
        target, experiment_edit_scope, mode=experiment_mode,
    )
    operator_brief = os.environ.get("DALEK_EXPERIMENT_BRIEF", "").strip()
    if operator_brief:
        experiment_block = (
            experiment_block.rstrip()
            + "\n\n## Operator proof-thread brief\n\n"
            + operator_brief
            + "\n"
        )
    # Conditionally inject the "Decompose hard admits" guidance only for hard
    # targets, so easy targets don't carry its ~57 eager lines every round.
    # Whole-crate cuts (field-floor, bridge-*) are inherently broad multi-file
    # reconstructions whose hard admits live in dep files, NOT the anchor — so
    # the target-file-only `target_needs_decompose` check wrongly omitted the
    # guidance for them (corefloor_006: "no hard function detected" because
    # ristretto.rs had none, while the cone had 47). Force it on for those modes.
    if target_needs_decompose(target) or experiment_mode in _WHOLE_CRATE_MODES:
        decompose_block = DECOMPOSE_TEMPLATE.read_text().rstrip()
        why = ("whole-crate cut" if experiment_mode in _WHOLE_CRATE_MODES
               else "hard target")
        print(f"[run] injecting Decompose-hard-admits guidance ({why})",
              flush=True)
    else:
        decompose_block = ""
        print("[run] Decompose guidance omitted (no hard function detected)",
              flush=True)
    prompt = render_prompt(
        target=target, project=project, module=module,
        spec_snapshot=spec_snapshot, catalog_cache=catalog_cache,
        results_root=results_root, failure_block=failure_block,
        vstd_root=vstd_root, experiment_block=experiment_block,
        decompose_block=decompose_block,
        whole_crate_assignment=experiment_mode in _WHOLE_CRATE_MODES,
        verus_rlimit=verus_rlimit,
    )

    # Save the rendered prompt for reproducibility
    (tdir / "prompt_rendered.md").write_text(prompt)

    # Files whose per-round state we snapshot (and roll back to on a budget /
    # hang bail-out). Beyond the target + siblings, this includes any
    # experiment allow-edit dep that isn't already a sibling: in bridge-full /
    # contract-only the agent rewrites those dep files, so they must appear in
    # the round history diff AND must roll back on a bail-out — otherwise an
    # edited-but-broken dep is left in the worktree and the next round loses its
    # diff context. (The spec snapshot already covers these for the integrity
    # gate; this is the file-state parallel.) Deduped by resolved path.
    snapshot_targets: list[Path] = [target, *siblings]
    _snap_seen = {p.resolve() for p in snapshot_targets}
    for _p in (experiment_allow_edit or []):
        if _p.resolve() not in _snap_seen:
            snapshot_targets.append(_p)
            _snap_seen.add(_p.resolve())

    # Per-round file snapshots so we can diff "what the previous round
    # tried" and surface it back to the agent. "round_0" captures the
    # baseline before any agent edits.
    snapshots_root = tdir / "snapshots"
    snapshot_files(snapshot_targets, snapshots_root / "round_0")

    start = datetime.now()
    round_results: list[RoundResult] = []
    end_reason: Optional[str] = None
    last_verus_err = ""
    last_failed_decls: list[str] = []   # Feature 1: decls verus rejected last round
    last_failed_locs: list[tuple[str, int]] = []  # Feature 1: (file, line) of those errors
    # Continuation message used on round 2+. Updated below when the
    # previous round's COMPLETE was rejected, so the agent sees the
    # specific reason (verus failure or admits remaining) prepended to
    # the round-history block instead of silently re-trying the same path.
    next_continue_msg = "continue"

    # Lever 2 — auto-reset bookkeeping. Default values mean no reset;
    # populated and consumed by the after-round decision below.
    session_start_round = 1
    fresh_next_round = False
    auto_resets_used = 0
    session_cc_tokens = 0
    reset_round_starts: list[int] = []
    # GIT_RECOVERY softening: a peek into git history is no longer instantly
    # terminal. We discard the contaminated round + reset the session, up to
    # this many times; only a repeat offender past the cap fails the task.
    git_recovery_resets = 0
    # FROZEN_EDIT softening: a misplaced helper lemma is no longer instantly
    # terminal. We revert the frozen file(s) to baseline + ask the agent to
    # relocate the work, up to this many times; only a repeat offender past the
    # cap fails the task. (The post-loop frozen gate still blocks any frozen
    # edit that survives to the end, so terminal safety is preserved.)
    frozen_edit_resets = 0
    # SPEC_DRIFT softening: restore frozen specs and continue when possible,
    # but cap repeats by drifted construct so a persistent spec fighter still
    # fails terminally.
    spec_drift_recovery_counts: dict[str, int] = {}
    max_spec_drift_recoveries = 3
    # E7: verified-false / unconfirmed-false reconstructed contracts (populated
    # only if the agent escalates FALSE_CONTRACT and run.py verifies the witness).
    false_contracts: list[dict] = []
    unconfirmed_false: list[dict] = []
    # Plateau guard: track the best (lowest) active progress metric and how many
    # rounds have passed with no NEW low. Admit count is primary, but whole-crate
    # modes switch to raw Verus errors once hard admits reach 0; otherwise a
    # zero-admit run can never improve and stops while proofs are still moving.
    # After plateau_reset_rounds with no new low we force a fresh session + a
    # targeted directive; after plateau_stop_rounds we stop (LIMIT).
    plateau_metric_name: Optional[str] = None
    plateau_best_value: Optional[int] = None
    rounds_since_new_low = 0
    plateau_reset_rounds = max(3, plateau_stop_rounds // 2)
    plateau_directive: Optional[str] = None   # set on plateau, injected next round
    plateau_stop_now = False                  # set mid-round, break after gates
    # Last round number whose end-state was verus_okay (used as the
    # rollback target if the budget exhausts mid-fix and leaves the
    # file in a broken state). 0 = the pre-round baseline.
    last_good_snapshot_round = 0

    # Explicit session id for this task. Used with `--session-id <uuid>` on
    # the first round (pins the new session to this UUID) and `--resume
    # <uuid>` on subsequent rounds (continues exactly this session, not
    # whatever `claude -c` picks as "most recent"). Regenerated on each
    # Lever 2 auto-reset.
    task_session_id = str(uuid.uuid4())

    def _admit_count() -> int:
        # Count across the SAME scope as the COMPLETE gate (_count_gate_admits:
        # target + experiment_allow_edit deps), so round-progress logging and
        # stall detection track the admits the gate actually cares about. In
        # proof-only / whole-crate modes the actionable admits live in the dep files,
        # not the target, so a target-only count would report "0 left / no
        # progress" while the agent is genuinely filling dep bodies.
        try:
            return _count_gate_admits(target, experiment_allow_edit)
        except OSError:
            return -1

    # Axiom-integrity gate: snapshot the set of `proof fn axiom_*` names
    # across every file the agent may touch (target + siblings + any
    # experiment allow-edit deps). The COMPLETE counter excludes admits
    # inside axiom_* bodies, so a NEW axiom_* is a fake-green vector — the
    # agent could discharge a proof obligation through a fresh
    # `proof fn axiom_cheat() { admit() }`. Any name not in this baseline,
    # appearing later, fails the round like spec drift.
    axiom_scope_files = [target, *siblings, *(experiment_allow_edit or [])]

    def _axiom_names() -> set[str]:
        names: set[str] = set()
        for f in axiom_scope_files:
            try:
                names |= axiom_fn_names(f.read_text())
            except OSError:
                pass
        return names

    baseline_axioms = _axiom_names()

    # Sealed-worktree detection (peel orphan HEAD): when true, HEAD-relative git
    # commands can't leak the original, so the git-recovery gate only flags
    # explicit non-HEAD references — a diagnostic `git diff` no longer discards a
    # round of real progress.
    worktree_sealed = _is_sealed_worktree(project)
    if worktree_sealed:
        print("[run] worktree is sealed (orphan HEAD) — git-recovery gate "
              "relaxed to explicit non-HEAD refs only", flush=True)
        object_leaks, object_audit_error = _sealed_git_object_leaks(project)
        if object_leaks or object_audit_error:
            detail = object_audit_error or object_leaks[0]
            msg = (
                "sealed worktree git object store is not clean; "
                f"unreachable objects are a source-recovery oracle ({detail})"
            )
            print(f"[run] GIT_RECOVERY: {msg}", flush=True)
            task_result = TaskResult(
                task_id=target_id, run_id=run_id, target_path=str(target),
                module_path=module, success=False, end_reason="GIT_RECOVERY",
                rounds_used=0,
                duration_seconds=(datetime.now() - start).total_seconds(),
                error_message=msg,
                experiment_provenance=experiment_provenance,
            )
            write_json(tdir / "result.json", task_result)
            return task_result

    # Forbidden-construct integrity gate. `assume(...)` and
    # `#[verifier::external_body]` are prompt-forbidden (prompt.md) because each
    # discharges a proof obligation WITHOUT an SMT proof — `assume(false)`
    # closes any goal, an external_body fn skips its body entirely — and neither
    # leaves an `admit()` or a new `axiom_*` for the COMPLETE gate's counters to
    # catch. A new `lemma_*` whose body is `assume(false)`, or a new
    # external_body helper, is therefore a fake-green vector in exactly the
    # class SPEC_DRIFT / AXIOM_DRIFT / TOOLING_DRIFT guard against. Snapshot a
    # comment/string-aware count across the editable scope (target + siblings +
    # allow-edit deps) before the loop; any INCREASE fails the round. This is
    # the only gate covering external_body in experiment mode, where the spec
    # gate (which catches external_body on the spec-snapshot path) is off.
    # Baseline-diff (not absolute) so a pre-existing `external_body` in seeded
    # dalek source doesn't trip it — only agent-introduced constructs do.
    forbidden_scope_files = [target, *siblings, *(experiment_allow_edit or [])]

    def _forbidden_counts() -> dict[str, int]:
        totals = {"assume": 0, "external_body": 0}
        for f in forbidden_scope_files:
            try:
                c = count_forbidden_constructs(f.read_text())
            except OSError:
                continue
            for k in totals:
                totals[k] += c.get(k, 0)
        return totals

    baseline_forbidden = _forbidden_counts()

    def _forbidden_introduced() -> list[str]:
        """Forbidden constructs introduced this round vs the pre-run baseline,
        as human-readable `name (+N)` strings. Empty = none added."""
        cur = _forbidden_counts()
        return [f"{k} (+{cur[k] - baseline_forbidden.get(k, 0)})"
                for k in sorted(cur)
                if cur[k] > baseline_forbidden.get(k, 0)]

    # Tooling-integrity gate: the harness's own verification skills are
    # re-read from disk every round — verus_check.py / spec_check.py run as
    # subprocesses in the round loop below, and the agent invokes the rest via
    # Bash. The proof agent reaches THIS repo's skills/ + lib/ through symlinks
    # in its scratch cwd (see _make_agent_cwd) and holds Edit/Write/Bash under
    # `--permission-mode bypassPermissions`, so it CAN rewrite a skill to always
    # return okay=true — the symlinks point at the real files, and this gate
    # hashes those same absolute HERE paths, so the edit is still caught. It is
    # an undetected fake-green vector otherwise: the
    # same cheat class the spec- and axiom-integrity gates exist to stop. (A
    # verus_check.py edit actually happened mid-run on this branch — that one
    # was a correct fix, but the hole is real.) Snapshot a content hash of
    # every tooling file under skills/ + lib/ at run start and diff after each
    # round; any add / edit / delete fails the round like SPEC_DRIFT. Note
    # tool-scoping (AGENT_TOOL_FLAGS) can't close this on its own: Bash is a
    # write primitive and `--allowedTools` is a no-op under bypassPermissions.
    def _tooling_digest() -> dict[str, str]:
        digest: dict[str, str] = {}
        for f in [*HERE.glob("skills/**/*.py"), *HERE.glob("lib/**/*.py")]:
            if "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(HERE))
            try:
                digest[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                digest[rel] = "MISSING"
        return digest

    baseline_tooling = _tooling_digest()

    def _tooling_changed() -> list[str]:
        """Tooling files (skills/ + lib/) whose content differs from the
        pre-run baseline — added, edited, or deleted. Empty = intact."""
        current = _tooling_digest()
        return sorted(
            k for k in (baseline_tooling.keys() | current.keys())
            if baseline_tooling.get(k) != current.get(k)
        )

    # Frozen-file guard (bridge-specs rung): the agent may edit ONLY the
    # allow-edit set; every other tracked file must stay byte-identical to
    # clean main. This is what makes the rung sound — the reconstructed map is
    # PINNED by frozen consumers (montgomery::to_edwards's proof, decompress's
    # contract, the curve lemmas), so the agent must not be able to weaken them
    # to fit a convenient definition. We diff the worktree against HEAD (the
    # committed clean main) and subtract the allow-edit files.
    try:
        _rc, _gr, _ = run_subskill(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"], env=env)
        _gitroot = Path(_gr.strip()) if _rc == 0 and _gr.strip() else project
    except Exception:
        _gitroot = project
    allow_edit_rel: set[str] = set()
    for _p in experiment_edit_scope:
        try:
            allow_edit_rel.add(str(_p.resolve().relative_to(_gitroot)))
        except ValueError:
            pass

    frozen_check_error: Optional[str] = None

    def _frozen_files_changed() -> list[str]:
        """Tracked files (vs HEAD = clean main) the agent changed OUTSIDE the
        allow-edit set. Empty = only the permitted file(s) moved."""
        nonlocal frozen_check_error
        changed, frozen_check_error = _frozen_paths_changed_from_git(
            project, allow_edit_rel, env=env)
        return changed

    def _revert_frozen_files(paths: list[str]) -> bool:
        """Best-effort restore of frozen files (gitroot-relative) to baseline.

        Frozen files are never in the editable set, so HEAD is their correct
        clean state — but the edit can take several shapes and `git checkout
        HEAD -- <p>` only handles some:
          - modified / deleted tracked file → `checkout HEAD` restores it;
          - added / renamed-new path (in the index but NOT in HEAD) → checkout
            FAILS ("did not match" / "pathspec"), so we `git rm -f` to undo the
            add instead.
        Per-path and rc-checked so a partial failure is visible. The CALLER
        must re-run `_frozen_files_changed()` afterwards and treat any residue
        as terminal — this function does not assert success on its own (a
        silently-failed revert that we then told the agent "was reverted" is a
        false-green vector). Runs from the git root so the gitroot-relative
        pathspecs resolve."""
        okay = True
        for p in paths:
            try:
                exists_rc, _, _ = run_subskill(
                    ["git", "-C", str(_gitroot), "cat-file", "-e", f"HEAD:{p}"],
                    env=env)
                if exists_rc == 0:
                    rc, _, _ = run_subskill(
                        ["git", "-C", str(_gitroot), "checkout", "HEAD", "--", p],
                        env=env)
                    if rc != 0:
                        okay = False
                        print(f"[run] FROZEN_EDIT: could not restore {p} "
                              f"from HEAD (checkout rc={rc})", flush=True)
                    continue
                # Not in HEAD (added/renamed-new): drop the add from index+tree.
                rc, _, _ = run_subskill(
                    ["git", "-C", str(_gitroot), "rm", "-f", "--ignore-unmatch",
                     "--", p],
                    env=env)
                if rc != 0:
                    okay = False
                    print(f"[run] FROZEN_EDIT: could not remove added path {p} "
                          f"(git rm rc={rc})", flush=True)
                leftover = _gitroot / p
                if leftover.exists():
                    try:
                        if leftover.is_dir():
                            shutil.rmtree(leftover)
                        else:
                            leftover.unlink()
                    except OSError as e:
                        okay = False
                        print(f"[run] FROZEN_EDIT: could not delete leftover "
                              f"{p}: {e!r}", flush=True)
            except Exception as e:  # never let a revert failure crash the loop
                okay = False
                print(f"[run] FROZEN_EDIT: revert of {p} raised {e!r}",
                      flush=True)
        return okay

    # Sibling-verify gate: track each sibling's content hash so we can
    # re-verify only the ones the agent actually modified each round.
    sibling_hashes: dict[str, str] = {}
    for s in siblings:
        try:
            sibling_hashes[str(s)] = _file_hash(s)
        except OSError:
            pass
    pending_sibling_fail: dict[str, dict] = {}

    for round_num in range(1, max_rounds + 1):
        print("=" * 60, flush=True)
        print(f"[run] round {round_num}/{max_rounds}", flush=True)
        admits_start = _admit_count()
        round_start = time.time()
        raw_out = tdir / "claude_raw" / f"round_{round_num}.jsonl"

        # Parent cwd = agent_cwd (a scratch dir outside the repo, symlinking
        # skills/ + lib/) so Claude Code does not inject HERE's CLAUDE.md.
        # Bash tool cwd = project root via BASH_ENV, so shell probes start where
        # the Rust source tree lives while the parent Claude process remains
        # outside the repo ancestry.
        # Compute remaining budget (wall-clock cap distributed across rounds).
        # If less than one productive round (~60s) remains, stop the loop
        # *before* invoking claude — otherwise we'd hand it a 60s deadline,
        # SIGKILL it, record a phantom rc=-9 / output_tokens=0 round, and
        # come back to do the same thing again next iteration. (Pre-fix
        # those zombie rounds ran past `max_task_minutes` and polluted
        # `round_results`; see docs/diagnostics.md.)
        elapsed = (datetime.now() - start).total_seconds()
        remaining_s = max_task_minutes * 60 - elapsed
        if remaining_s < 60.0:
            print(f"[run] budget exhausted "
                  f"(elapsed={elapsed:.0f}s ≥ {max_task_minutes*60:.0f}s, "
                  f"remaining={remaining_s:.0f}s < 60s) — stopping loop",
                  flush=True)
            break

        # Bail-out guard: if remaining budget is <5 min, no productive
        # agent work is possible (claude will be SIGKILL'd at ~60s by
        # the deadline, the floor). Two sub-cases:
        #
        #   (a) Current file state is broken (verus_okay=False on last
        #       round). Roll back to the latest verus_okay snapshot so
        #       we don't leave the file worse than admitted-start.
        #   (b) Current file state is clean. No rollback needed; just
        #       break out of the loop. Avoids burning N×60s rounds at
        #       end-of-budget with zero productive work.
        budget_exhausted = (max_task_minutes * 60 - elapsed) < 5 * 60
        last_verus_failed = bool(round_results) and not round_results[-1].verus_okay
        if budget_exhausted:
            if last_verus_failed:
                snap_dir = snapshots_root / f"round_{last_good_snapshot_round}"
                rollback_files = snapshot_targets
                if snap_dir.exists():
                    print(f"[run] budget exhausted with broken file state — "
                          f"rolling back to snapshots/round_{last_good_snapshot_round}/",
                          flush=True)
                    for f in rollback_files:
                        snap_f = snap_dir / _snapshot_name(f, rollback_files)
                        if snap_f.exists():
                            shutil.copy2(snap_f, f)
                    pending_sibling_fail.clear()
                else:
                    print(f"[run] budget exhausted with broken file state, but no "
                          f"snapshot to roll back to — file remains broken.",
                          flush=True)
                print(f"[run] bailing out of round loop early (budget < 5 min, "
                      f"file not verus_okay). end_reason=LIMIT", flush=True)
            else:
                print(f"[run] budget exhausted (remaining < 5 min); file is in "
                      f"verus_okay state with {_admit_count()} admits remaining. "
                      f"Bailing out of round loop to avoid wasted 60s rounds. "
                      f"end_reason=LIMIT", flush=True)
            break

        # For round 2+: assemble a "round history" message containing diffs
        # of the previous round(s) plus their verus errors. Delivered as
        # the next user message via claude -c -p <msg>. If the previous
        # round's END_REASON:COMPLETE was rejected by the harness's
        # final-state gate, prepend the specific rejection reason so the
        # agent doesn't silently retry the same self-declared COMPLETE.
        continue_message: Optional[str] = None
        if round_num > 1:
            history = build_round_history_block(
                tdir, round_num, target=target,
                since_round=session_start_round,
                # Whole-crate cuts must see target-file failures that have no
                # admit() (stripped API proofs) — don't filter them out.
                filter_target_errors=experiment_mode not in _WHOLE_CRATE_MODES,
                work_files=_round_history_work_files(
                    experiment_mode, experiment_edit_scope),
            )
            inventory = build_admit_inventory_block(target, experiment_allow_edit)
            parts: list[str] = []
            if plateau_directive:
                parts.append(plateau_directive)
                plateau_directive = None   # one-shot — consumed this round
            if next_continue_msg != "continue":
                parts.append(next_continue_msg)
            if inventory:
                parts.append(inventory)
            if history:
                admits_now = _admit_count()
                parts.append(
                    "Harness feedback for this round:\n\n" + history +
                    _continue_work_tail(experiment_mode, admits_now)
                )
            if parts:
                continue_message = "\n\n".join(parts)
                (tdir / f"round_history_{round_num}.md").write_text(continue_message)
            else:
                continue_message = "continue"

        # Lever 2: when fresh_next_round is set, mint a NEW session id and
        # start fresh (no `--resume`). File state on disk is preserved.
        use_resume = (round_num > 1 and not fresh_next_round)
        round_prompt = prompt
        if fresh_next_round:
            task_session_id = str(uuid.uuid4())
            agent_cwd = _make_agent_cwd(target_id)
            print(f"[run] starting FRESH claude session "
                  f"(auto-reset #{auto_resets_used}, session_id={task_session_id})",
                  flush=True)
            round_prompt = _fresh_session_prompt(
                prompt,
                continue_message,
                _render_claude_memory_carryover(tdir),
            )
        reason, rc, claude_result = run_claude_round(
            prompt=round_prompt,
            cwd=agent_cwd, env=env, raw_out=raw_out,
            session_id=task_session_id,
            resume=use_resume,
            model=model,
            deadline_seconds=remaining_s,
            continue_message=continue_message if use_resume else None,
        )
        duration = time.time() - round_start
        fresh_next_round = False
        memory_snapshot = _snapshot_claude_memory(raw_out, tdir, round_num)
        if memory_snapshot:
            print(f"[run] Claude memory snapshot -> {memory_snapshot}", flush=True)
        agent_state = snapshot_post_agent_round_state(
            project, snapshot_targets, snapshots_root, tdir, round_num, target)
        changed_count = len(agent_state.get("diff_name_status") or [])
        if changed_count:
            print(f"[run] post-agent state snapshot -> "
                  f"{agent_state.get('manifest_path')} "
                  f"({changed_count} changed path(s))", flush=True)

        # Deterministic rate-limit halt. A 429 means the API rejected the
        # request outright (5-hour session limit, quota exhausted, overage
        # disabled). Unlike the heuristic RATE_LIMIT_OR_HANG guard below
        # (which needs duration > 300), a rejection is instant (~2s) and
        # carries an explicit status, so the heuristic never catches it —
        # the auto-reset machinery instead reads the instant no-op as a
        # "stall," burns every remaining round, and exits as a plausible
        # LIMIT. Catch it here BEFORE the verus gate so a zero-hard-admit
        # target can't be stamped COMPLETE off a round the agent never ran,
        # and so the whole sweep can stop (every later round/target would be
        # rejected too until the window resets).
        if (claude_result.get("is_error")
                and claude_result.get("api_error_status") == 429):
            msg = claude_result.get("result", "rate limited")
            print(f"[run] round {round_num}: API rejected with 429 "
                  f"({msg!r}) — no work possible until the quota window "
                  f"resets. Aborting run (RATE_LIMITED).", flush=True)
            end_reason = "RATE_LIMITED"
            break

        # If Claude exits nonzero without a final `type:"result"` event, the
        # round never produced agent output. Do not run spec/verus gates over the
        # unchanged (or partially changed) files and misclassify the transport
        # failure as a proof LIMIT/no-op stall.
        no_result_exit = _classify_claude_no_result_exit(rc, claude_result, raw_out)
        if no_result_exit:
            git_recovery = detect_git_recovery(raw_out, sealed=worktree_sealed)
            process_crosstalk = detect_process_crosstalk(raw_out)
            if git_recovery:
                end_reason = "GIT_RECOVERY"
                taint_msg = (
                    f"; raw stream also contains git source recovery "
                    f"({len(git_recovery)} command(s), e.g. "
                    f"{git_recovery[0]!r})"
                )
            elif process_crosstalk:
                end_reason = "PROCESS_CROSSTALK"
                taint_msg = (
                    f"; raw stream also contains unsafe process/tmp control "
                    f"({len(process_crosstalk)} hit(s), e.g. "
                    f"{process_crosstalk[0]!r})"
                )
            else:
                end_reason = no_result_exit["reason"]
                taint_msg = ""
            msg = no_result_exit["message"]
            print(f"[run] round {round_num}: {msg}{taint_msg}. "
                  f"Aborting run ({end_reason}).", flush=True)
            last_verus_err = f"{msg}{taint_msg}"
            no_result_data = f"{msg}{taint_msg}"
            raw_usage_summary = summarize_raw_usage(raw_out)
            partial_snapshot = snapshot_partial_round_state(
                snapshot_targets, snapshots_root, round_num)
            no_result_spec_diag = None
            if not no_spec_gate:
                no_result_spec_diag = _run_spec_drift_diagnostic(
                    target, spec_snapshot, env, experiment_edit_scope)
                write_json(tdir / f"round_{round_num}_spec_drift.json",
                           no_result_spec_diag)
                if no_result_spec_diag.get("blocking_drift"):
                    print(f"[run] round {round_num}: no-result partial state "
                          f"has {len(no_result_spec_diag['blocking_drift'])} "
                          f"blocking spec drift item(s); preserving "
                          f"end_reason={end_reason}", flush=True)
            rr = RoundResult(
                round_number=round_num,
                end_reason=end_reason,
                returncode=rc,
                duration_seconds=duration,
                verus_okay=False,
                verus_error_count_raw=1,
                verification_error_count=0,
                diagnostic_kind_counts={"transport": 1},
                has_build_wrapper=False,
                compile_blocked_or_indeterminate=True,
                verus_errors=[{
                    "file": str(raw_out),
                    "line": 0,
                    "column": 0,
                    "data": no_result_data,
                    "partial_snapshot": partial_snapshot.get("snapshot_dir", ""),
                    "spec_drift_diagnostic": (
                        str(tdir / f"round_{round_num}_spec_drift.json")
                        if no_result_spec_diag is not None else ""
                    ),
                }],
                spec_drift=(
                    no_result_spec_diag.get("blocking_drift", [])
                    if no_result_spec_diag else []
                ),
                claude_usage={},
                raw_usage_summary=raw_usage_summary,
                agent_delegations=count_agent_delegations(raw_out),
            )
            round_results.append(rr)
            write_json(tdir / f"round_{round_num}.json", rr)
            break

        # Spec drift gate (skipped in experiment mode — agent is expected to
        # add specs back to dependency files; snapshot above is still kept
        # for post-hoc analysis against the original).
        if no_spec_gate:
            spec_drift = []
        else:
            # When the gate is on, specs are frozen — so freeze spec fn
            # DEFINITIONS (bodies) too, not just headers. Otherwise a spec fn
            # co-located in an editable file (e.g. edwards.rs's open spec fns,
            # or the lemma files in --strip-to-fields) could be redefined to
            # hollow out a frozen contract without tripping a header check.
            # Folds into spec_drift → SPEC_DRIFT (non-promotable).
            rc_spec, spec_stdout, _ = run_subskill(
                [sys.executable, str(HERE / "skills" / "spec_check.py"),
                 "verify", str(target), "--against", str(spec_snapshot),
                 "--check-spec-defs"],
                env=env,
            )
            try:
                spec_drift = json.loads(spec_stdout).get("drift", [])
            except json.JSONDecodeError:
                spec_drift = []
            if spec_drift:
                allowed_generated_drift, spec_drift = (
                    _partition_generated_contract_drift(
                        spec_drift, experiment_edit_scope)
                )
                if allowed_generated_drift:
                    names = sorted({
                        f"{Path(d.get('file', '')).name}::{d.get('function')}"
                        for d in allowed_generated_drift
                    })
                    print(f"[run] GENERATED_CONTRACT: allowed contract drift "
                          f"for {names}; remaining_drift={len(spec_drift)}",
                          flush=True)

        if spec_drift:
            print(f"[run] round {round_num}: SPEC_DRIFT has "
                  f"{len(spec_drift)} blocking item(s); skipping Verus check "
                  "until frozen specs are restored.", flush=True)
            verus_result = _spec_drift_verus_result(spec_drift)
            plateau_verus_result = {
                **verus_result,
                "messages": list(verus_result.get("messages", []) or []),
            }
        else:
            # Verus check on the target (anchor) module. Whole-crate modes must
            # use Verus's default SMT rlimit first; a global high rlimit can
            # mask source errors as a broad timeout (Phase A 057).
            verus_cmd = _harness_verus_command(
                target, project, experiment_mode, verus_rlimit)
            rc_verus, verus_stdout, _ = run_subskill(verus_cmd, env=env)
            try:
                verus_result = json.loads(verus_stdout)
            except json.JSONDecodeError:
                verus_result = {"okay": False, "messages": []}
            plateau_verus_result = {
                **verus_result,
                "messages": list(verus_result.get("messages", []) or []),
            }

            # In experiment mode the prompt instructs the agent to drive
            # verus_check on each dep file separately (`--verify-module`
            # is module-scoped, so the anchor check alone can't see errors in
            # dep proof bodies). Mirror that on the harness side so the
            # `verus_okay` signal that gates COMPLETE reflects the same truth
            # the agent is being asked to verify. Without this the loop
            # cannot exit early: an honest LIMIT can never flip to COMPLETE.
            # In _WHOLE_CRATE_MODES the anchor check above is already --whole-crate,
            # so it authoritatively verifies every dep in the crate module tree and
            # this sweep is redundant (the preceding sweep rationale assumes a
            # module-scoped anchor). Skipping it avoids both an empty editable module
            # failing "could not find module" -> forcing verus_okay False forever
            # (a COMPLETE-blocker) and 10-20 min/round of redundant checks. Assumes
            # allow_edit is within the crate module tree.
            deps_to_check = () if experiment_mode in _WHOLE_CRATE_MODES \
                else (experiment_allow_edit or [])
            for dep in deps_to_check:
                dep_cmd = [sys.executable, str(HERE / "skills" / "verus_check.py"),
                           str(dep), "--project", str(project)]
                if verus_rlimit is not None:
                    dep_cmd += ["--rlimit", str(verus_rlimit)]
                rc_dep, dep_stdout, _ = run_subskill(dep_cmd, env=env)
                try:
                    dep_result = json.loads(dep_stdout)
                except json.JSONDecodeError:
                    dep_result = {"okay": False, "messages": []}
                _merge_plateau_verus_result(plateau_verus_result, dep_result)
                if not dep_result.get("okay", False):
                    verus_result["okay"] = False
                verus_result.setdefault("messages", []).extend(
                    dep_result.get("messages", [])[:5]
                )

        last_failed_decls = verus_result.get("failed_declarations", []) or []  # Feature 1
        # Feature 1: error (file, line) locations — the reliable signal for
        # mapping a failure back to its fn body (see _extract_near_miss).
        last_failed_locs = [(m.get("file", ""), m.get("line", 0))
                            for m in verus_result.get("messages", [])
                            if m.get("line")]

        claude_usage = {}
        if claude_result:
            u = claude_result.get("usage") or {}
            claude_usage = {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "total_cost_usd": claude_result.get("total_cost_usd", 0.0),
            }
        raw_usage_summary = summarize_raw_usage(raw_out)

        rr = RoundResult(
            round_number=round_num,
            end_reason=reason,
            returncode=rc,
            duration_seconds=duration,
            verus_okay=verus_result.get("okay", False),
            verus_error_count_raw=_verus_error_count(verus_result),
            verification_error_count=_verification_error_count(verus_result),
            verified_count=verus_result.get("verified_count"),
            raw_verus_error_count=verus_result.get("raw_verus_error_count"),
            diagnostic_kind_counts=_diagnostic_kind_counts(verus_result),
            has_build_wrapper=(
                _diagnostic_kind_counts(verus_result).get("build-wrapper", 0) > 0
            ),
            compile_blocked_or_indeterminate=(
                _compile_blocked_or_indeterminate(verus_result)
            ),
            truncated=bool(verus_result.get("truncated", False)),
            # Diversify across files (not first-20) so a whole-crate check whose
            # first 20 errors are all one module doesn't drop every other
            # module's diagnostics — the corefloor_006 truncation bug.
            verus_errors=_diversify_messages(verus_result.get("messages", [])),
            spec_drift=spec_drift,
            claude_usage=claude_usage,
            raw_usage_summary=raw_usage_summary,
            agent_delegations=count_agent_delegations(raw_out),
        )
        last_verus_err = _format_diagnostics_for_memory(
            rr.verus_errors, verus_result.get("stderr_tail", "") or "")
        round_results.append(rr)
        write_json(tdir / f"round_{round_num}.json", rr)

        # Capture end-of-round file state for next round's history diff.
        snapshot_files(snapshot_targets,
                       snapshots_root / f"round_{round_num}")

        # Sibling-verify gate. The per-round verus check above covers only
        # the TARGET module. Rule 4 lets the agent edit sibling helpers under
        # lemmas/<area>_lemmas/; a bad sibling edit can break that sibling's
        # OWN verification, or break a top-level module that consumes it,
        # without the target-only check noticing. Re-verify each sibling
        # modified this round plus its area's top-level module(s).
        sibling_fail: list[dict] = []
        if _should_run_sibling_verify(sibling_verify, experiment_mode):
            modified_sibs = []
            for s in siblings:
                try:
                    h = _file_hash(s)
                except OSError:
                    continue
                if h != sibling_hashes.get(str(s)):
                    modified_sibs.append(s)
                    sibling_hashes[str(s)] = h
            reverify: set[Path] = set()
            for s in modified_sibs:
                reverify.add(s)
                for mod in _area_top_level_modules(s):
                    tl = project / "src" / (mod.replace("::", "/") + ".rs")
                    if tl.exists():
                        reverify.add(tl)
            reverify.discard(target)  # target already checked above
            for f in sorted(reverify):
                sv_cmd = [sys.executable, str(HERE / "skills" / "verus_check.py"),
                          str(f), "--project", str(project)]
                if verus_rlimit is not None:
                    sv_cmd += ["--rlimit", str(verus_rlimit)]
                _, sv_out, _ = run_subskill(sv_cmd, env=env)
                try:
                    sv_res = json.loads(sv_out)
                except json.JSONDecodeError:
                    sv_res = {"okay": False, "messages": []}
                if not sv_res.get("okay"):
                    sibling_fail.append({
                        "file": str(f.relative_to(project)),
                        "errors": sv_res.get("messages", [])[:5],
                    })
                    pending_sibling_fail[str(f.relative_to(project))] = sibling_fail[-1]
                else:
                    pending_sibling_fail.pop(str(f.relative_to(project)), None)
            if sibling_fail:
                print(f"[run] round {round_num}: sibling re-verify FAILED for "
                      f"{[d['file'] for d in sibling_fail]} — "
                      f"continuing with sibling diagnostics", flush=True)

        if pending_sibling_fail:
            active_sibling_fail = list(pending_sibling_fail.values())
            sibling_messages = _flatten_sibling_fail_messages(active_sibling_fail)
            rr.verus_okay = False
            rr.verus_errors = _diversify_messages(
                [*rr.verus_errors, *sibling_messages])
            last_verus_err = _format_diagnostics_for_memory(
                rr.verus_errors, verus_result.get("stderr_tail", "") or "")
            last_failed_locs = [(m.get("file", ""), m.get("line", 0))
                                for m in rr.verus_errors
                                if isinstance(m, dict) and m.get("line")]
            write_json(tdir / f"round_{round_num}.json", rr)

        admits_end = _admit_count()
        admits_delta = (admits_start - admits_end) if (admits_start >= 0 and admits_end >= 0) else 0

        # Plateau guard: a NEW low resets the counter; otherwise it climbs. Using
        # "no new low" (not "Δ0 this round") tolerates a temporary rise when the
        # agent adds a helper lemma with a transient admit while still making net
        # progress elsewhere. In zero-admit whole-crate modes, switch to
        # source-span verifier errors; resume12 was still reducing those when
        # admit-keyed plateau logic stopped it early. Timeout/build-only rounds
        # are indeterminate and leave the plateau state unchanged.
        if admits_end >= 0:
            if _plateau_metric_indeterminate(
                experiment_mode, admits_end, plateau_verus_result
            ):
                print(f"[run] round {round_num}: plateau metric held "
                      f"(failed whole-crate check produced no source-span "
                      f"verification errors)", flush=True)
            else:
                metric_name, metric_value = _plateau_progress_metric(
                    experiment_mode, admits_end, plateau_verus_result)
                (plateau_metric_name,
                 plateau_best_value,
                 rounds_since_new_low) = _update_plateau_progress(
                    plateau_metric_name,
                    plateau_best_value,
                    rounds_since_new_low,
                    metric_name,
                    metric_value,
                )

        # Lever 2: update per-session token counter
        cc_this_round = claude_usage.get("cache_creation_input_tokens", 0)
        session_cc_tokens += cc_this_round

        print(f"[run] round {round_num}: end_reason={reason} "
              f"verus_okay={rr.verus_okay} spec_drift={len(spec_drift)} "
              f"admits {admits_start}→{admits_end} (Δ{-admits_delta if admits_delta else 0}) "
              f"cc_tokens={cc_this_round/1000:.0f}k (session_cum={session_cc_tokens/1000:.0f}k) "
              f"agent_delegations={rr.agent_delegations}",
              flush=True)

        # Rate-limit / hang detection. We want to bail out when the agent
        # literally did nothing in this round (likely the Claude API is
        # throttling, or the subprocess hung). The correct productivity
        # signal is "did the agent perform any tool_use or emit any text"
        # — counted directly from the raw jsonl. `cc_tokens` is NOT
        # reliable because it's 0 whenever the agent gets SIGKILL'd before
        # emitting a final `result` event, which happens routinely when
        # the wall-deadline fires mid-work.
        agent_actions = count_agent_actions(raw_out)
        if agent_actions == 0 and duration > 300 and rr.end_reason is None:
            print(f"[run] round {round_num}: agent performed 0 actions in {duration:.0f}s — "
                  f"likely rate-limited or hung. Bailing out of round loop. "
                  f"end_reason=RATE_LIMIT_OR_HANG", flush=True)
            # Roll back if the file is now broken (otherwise leave as-is).
            if not rr.verus_okay:
                snap_dir = snapshots_root / f"round_{last_good_snapshot_round}"
                if snap_dir.exists():
                    import shutil as _shutil
                    for f in snapshot_targets:
                        snap_f = snap_dir / _snapshot_name(f, snapshot_targets)
                        if snap_f.exists():
                            _shutil.copy2(snap_f, f)
                    pending_sibling_fail.clear()
                    print(f"[run] rolled back to snapshots/round_{last_good_snapshot_round}/",
                          flush=True)
            end_reason = "RATE_LIMIT_OR_HANG"
            break

        # Lever 2: auto-reset decision. Evaluate AFTER the round but BEFORE
        # the early-exit decision below (COMPLETE/SPEC_DRIFT should still
        # short-circuit). We only reset if we'd otherwise continue.
        if auto_reset and auto_resets_used < max_auto_resets and round_num < max_rounds:
            # Stall signal: last 2 rounds in current session, zero fills + short.
            stall = False
            session_rounds = [rr_ for rr_ in round_results
                              if rr_.round_number >= session_start_round]
            if len(session_rounds) >= 2:
                # Need admits_delta per round — derive from snapshots so we don't
                # have to plumb it through RoundResult. Sum across ALL snapshotted
                # files (target + siblings + allow-edit deps, the same scope as
                # _admit_count / the COMPLETE gate), resolving each snapshot file
                # through the same collision-free naming helper snapshot_files
                # uses — so in proof-only / whole-crate modes a fill in a dep file
                # counts as progress and doesn't read as a stall.
                def _admits_in_snap(n: int) -> int:
                    snap_dir = snapshots_root / f"round_{n}"
                    if not snap_dir.exists():
                        return -1
                    total, found = 0, False
                    for f in snapshot_targets:
                        sf = snap_dir / _snapshot_name(f, snapshot_targets)
                        if not sf.exists():
                            continue
                        try:
                            total += _count_llm_target_admits(sf.read_text())
                            found = True
                        except OSError:
                            pass
                    return total if found else -1
                last_two = session_rounds[-2:]
                d1_start = _admits_in_snap(last_two[0].round_number - 1)
                d1_end = _admits_in_snap(last_two[0].round_number)
                d2_start = _admits_in_snap(last_two[1].round_number - 1)
                d2_end = _admits_in_snap(last_two[1].round_number)
                if (d1_start == d1_end and d2_start == d2_end and
                    d1_start >= 0 and d2_start >= 0 and
                    last_two[0].duration_seconds < stall_max_duration_sec and
                    last_two[1].duration_seconds < stall_max_duration_sec):
                    stall = True

            # Bloat signal: cumulative cache_creation past threshold
            bloat = session_cc_tokens > bloat_threshold_tokens

            if stall or bloat:
                reason_str = []
                if stall: reason_str.append(
                    f"stall (rounds {round_num-1},{round_num}: 0 fills, "
                    f"dur<{stall_max_duration_sec/60:.0f}min)")
                if bloat: reason_str.append(
                    f"bloat (session_cc={session_cc_tokens/1000:.0f}k>"
                    f"{bloat_threshold_tokens/1000:.0f}k)")
                print(f"[run] auto-reset: round {round_num+1} → fresh session. "
                      f"reason={'; '.join(reason_str)}. "
                      f"resets_used={auto_resets_used+1}/{max_auto_resets}", flush=True)
                fresh_next_round = True
                session_start_round = round_num + 1
                session_cc_tokens = 0
                auto_resets_used += 1
                reset_round_starts.append(round_num + 1)

        # Plateau guard (duration-INDEPENDENT — the stall detector above only
        # fires on SHORT no-progress rounds, so it missed corefloor_006's long
        # expensive plateau). After plateau_reset_rounds with no new metric low,
        # force a fresh session + a strong targeted directive;
        # after plateau_stop_rounds, stop rather than burn more budget.
        # Detect the plateau stop here, but DON'T break yet — let the integrity
        # gates below (SPEC_DRIFT / FROZEN_EDIT / SIBLING_VERUS_FAIL / git-
        # recovery / …) run first so a cheat on the final plateau round is still
        # recorded with its real label, not masked as a benign LIMIT. The break
        # fires at the end of the loop body, only if no gate fired.
        plateau_stop_now = bool(
            plateau_stop_rounds and rounds_since_new_low >= plateau_stop_rounds)
        metric_label = plateau_metric_name or "progress metric"
        metric_best = plateau_best_value if plateau_best_value is not None else "?"
        if plateau_stop_now:
            print(f"[run] PLATEAU_STOP: {rounds_since_new_low} rounds with no "
                  f"{metric_label} reduction (best={metric_best}). Will stop "
                  f"after this round's integrity gates.", flush=True)
        if (plateau_stop_rounds and not fresh_next_round
                and rounds_since_new_low >= plateau_reset_rounds
                and round_num < max_rounds):
            print(f"[run] plateau: {rounds_since_new_low} rounds, no new "
                  f"{metric_label} low (best={metric_best}) → fresh session + "
                  f"plateau directive.", flush=True)
            fresh_next_round = True
            session_start_round = round_num + 1
            session_cc_tokens = 0
            reset_round_starts.append(round_num + 1)
            directive_best_admits = (
                0 if plateau_metric_name == "whole-crate source-span Verus errors"
                else plateau_best_value
            )
            plateau_directive = _plateau_directive_text(
                rounds_since_new_low, directive_best_admits, experiment_mode)

        # Decision
        process_crosstalk = detect_process_crosstalk(raw_out)
        if process_crosstalk:
            print(f"[run] PROCESS_CROSSTALK: agent used unsafe process/tmp "
                  f"control ({len(process_crosstalk)} hit(s), e.g. "
                  f"{process_crosstalk[0]!r}). Failing task.", flush=True)
            rr.verus_okay = False
            rr.end_reason = "PROCESS_CROSSTALK"
            rr.verus_errors = _diversify_messages([
                *rr.verus_errors,
                {
                    "file": str(raw_out),
                    "line": 0,
                    "column": 0,
                    "data": (
                        "agent used unsafe process/tmp control: "
                        f"{process_crosstalk[0]}"
                    ),
                },
            ])
            write_json(tdir / f"round_{round_num}.json", rr)
            last_verus_err = rr.verus_errors[-1]["data"]
            end_reason = "PROCESS_CROSSTALK"
            break

        git_recovery = detect_git_recovery(raw_out, sealed=worktree_sealed)
        if git_recovery:
            # The agent recovered original source from version control. The
            # experiment strips the WORKING TREE, but the worktree's history
            # (and the shared object store) can still hold the full original
            # proof + lemmas; `git show HEAD:<file>` + copy-back reproduces the
            # answer verbatim, so a green verus would be retrieval, not
            # reconstruction. This is a fake-green vector — checked first,
            # before any gate that trusts verus_okay.
            #
            # It is NOT instantly terminal. Killing the whole task on one peek
            # wastes the budget and the prior rounds' honest progress. Instead
            # we treat the peek as contamination of THIS round only: discard the
            # round's edits (they may be the retrieved answer copied back) by
            # rolling the editable files back to the start-of-round snapshot
            # (= end of round_{N-1}, which preserves earlier honest progress),
            # and force a FRESH session next round so the leaked content leaves
            # the model's context. A green produced after that is reconstruction
            # again, not recall. Only a repeat offender past the cap — i.e. the
            # agent keeps reaching for git even after rollback+reset — fails the
            # task with the terminal GIT_RECOVERY label.
            git_recovery_resets += 1
            print(f"[run] GIT_RECOVERY: agent read original source from git "
                  f"({len(git_recovery)} command(s), e.g. {git_recovery[0]!r}). "
                  f"recoveries={git_recovery_resets}/{max_git_recovery_resets}",
                  flush=True)
            if git_recovery_resets > max_git_recovery_resets:
                print("[run] GIT_RECOVERY: cap exceeded — repeated retrieval "
                      "after rollback+reset. Failing task.", flush=True)
                end_reason = "GIT_RECOVERY"
                break
            rb_dir = snapshots_root / f"round_{round_num - 1}"
            if rb_dir.exists():
                for f in snapshot_targets:
                    snap_f = rb_dir / _snapshot_name(f, snapshot_targets)
                    if snap_f.exists():
                        shutil.copy2(snap_f, f)
                print(f"[run] GIT_RECOVERY: discarded this round's edits, rolled "
                      f"back to snapshots/round_{round_num - 1}/", flush=True)
            else:
                print(f"[run] GIT_RECOVERY: no round_{round_num - 1} snapshot to "
                      f"roll back to (round 1?); leaving working tree as-is.",
                      flush=True)
            # Fresh session next round (sheds the leaked context); mirror the
            # auto-reset bookkeeping so stall detection re-baselines too.
            fresh_next_round = True
            session_start_round = round_num + 1
            session_cc_tokens = 0
            reset_round_starts.append(round_num + 1)
            # Taint this round: `verus_okay` was computed before the rollback
            # and before resetting away leaked context. If this is the final
            # iteration, it must not satisfy the final COMPLETE gate.
            rr.verus_okay = False
            rr.end_reason = "GIT_RECOVERY_RECOVERED"
            write_json(tdir / f"round_{round_num}.json", rr)
            next_continue_msg = (
                "STOP reading version control. Your previous round ran a git "
                "command that recovers the original source (show/restore/"
                "checkout/log -p/diff/cat-file). That is retrieval, not "
                "reconstruction — those edits were discarded and the session "
                "was reset. Reconstruct the proofs from the frozen specs and "
                "lemmas only; never read .git or any committed/historical "
                "version of these files. Repeating this fails the task.")
            continue
        changed_tooling = _tooling_changed()
        if changed_tooling:
            # Hard fail: the agent altered the harness's own verification
            # tooling. A doctored verus_check / spec_check / admit counter can
            # fake a green, which means THIS round's verus_okay & spec_drift
            # signals are themselves untrustworthy — so this is checked first,
            # before any gate that consumed those signals. Same cheat class as
            # spec / axiom drift: break and record it.
            print(f"[run] TOOLING_DRIFT: agent modified harness tooling "
                  f"{changed_tooling} — a verification skill that always "
                  f"returns okay=true is a fake-green vector. Failing round.",
                  flush=True)
            end_reason = "TOOLING_DRIFT"
            break
        if spec_drift:
            labels = [_spec_drift_label(d) for d in spec_drift]
            for label in labels:
                spec_drift_recovery_counts[label] = (
                    spec_drift_recovery_counts.get(label, 0) + 1)
            over_cap = [
                label for label in labels
                if spec_drift_recovery_counts.get(label, 0) > max_spec_drift_recoveries
            ]
            print(f"[run] SPEC_DRIFT: restoring frozen spec surface for "
                  f"{labels[:5]}{'...' if len(labels) > 5 else ''}; "
                  f"recoveries={max(spec_drift_recovery_counts[l] for l in labels)}/"
                  f"{max_spec_drift_recoveries}", flush=True)
            if over_cap:
                print(f"[run] SPEC_DRIFT: cap exceeded for {over_cap} — "
                      "agent kept modifying the same frozen spec after restore "
                      "+ feedback. Failing task.", flush=True)
                rr.end_reason = "SPEC_DRIFT"
                rr.verus_okay = False
                write_json(tdir / f"round_{round_num}.json", rr)
                end_reason = "SPEC_DRIFT"
                break
            recovery = _restore_spec_drift_from_baseline(
                spec_drift, snapshot_targets, snapshots_root, target,
                spec_snapshot, env, experiment_edit_scope)
            write_json(tdir / f"round_{round_num}_spec_drift_recovery.json",
                       recovery)
            if not recovery.get("okay"):
                print(f"[run] SPEC_DRIFT: restore failed or residual drift "
                      f"remains (unresolved={recovery.get('unresolved', [])}, "
                      f"residual={len(recovery.get('residual_drift', []) or [])}) "
                      "— failing task.", flush=True)
                rr.end_reason = "SPEC_DRIFT"
                rr.verus_okay = False
                write_json(tdir / f"round_{round_num}.json", rr)
                end_reason = "SPEC_DRIFT"
                break
            # Taint this round: Verus was skipped on the drifted state, and the
            # restored state has not yet been verified. COMPLETE requires a later
            # clean round after the restore.
            rr.verus_okay = False
            rr.end_reason = "SPEC_DRIFT_RECOVERED"
            restored_files = recovery.get("restored_files", []) or []
            rr.verus_errors = _diversify_messages([
                *rr.verus_errors,
                {
                    "file": restored_files[0] if restored_files else str(target),
                    "line": 0,
                    "column": 0,
                    "data": (
                        "SPEC_DRIFT recovered: frozen specs were restored; "
                        "continue against the restored contracts."
                    ),
                },
            ])
            snapshot_files(snapshot_targets,
                           snapshots_root / f"round_{round_num}")
            write_json(tdir / f"round_{round_num}.json", rr)
            next_continue_msg = _spec_drift_continue_msg(
                spec_drift, restored_files)
            continue
        spec_drift_recovery_counts.clear()
        if experiment_mode in _WHOLE_CRATE_MODES:
            frozen_changed = _frozen_files_changed()
            if frozen_check_error:
                print(f"[run] FROZEN_EDIT: frozen-file audit failed "
                      f"({frozen_check_error}) — cannot prove frozen files "
                      "stayed unchanged. Failing task.", flush=True)
                end_reason = "FROZEN_EDIT"
                break
            if frozen_changed:
                # Recoverable: a frozen-file edit is misplaced legitimate work,
                # not a proof-bypass cheat. The cheat vectors that freezing
                # guards against — a weakened spec/contract redefining the
                # guarantee — are caught terminally by SPEC_DRIFT above; a
                # *new* helper lemma dumped into a frozen file is just in the
                # wrong place. Killing the run on the first misplacement wastes
                # the budget AND the agent's real proof (resume14 lost a
                # 748-line Montgomery ladder this way). Instead revert ONLY the
                # frozen files to baseline — restoring the frozen guarantee
                # while keeping this round's editable progress — tell the agent
                # to relocate the work into an editable file, and continue. No
                # session reset: the agent still holds the proof in context and
                # can re-emit it into a legal file. Only a repeat offender past
                # the cap fails terminally; and the post-loop frozen gate still
                # blocks any frozen edit that survives to the end.
                frozen_edit_resets += 1
                print(f"[run] FROZEN_EDIT: agent modified frozen file(s) "
                      f"{frozen_changed} — reverting to baseline. "
                      f"resets={frozen_edit_resets}/{max_frozen_edit_resets}",
                      flush=True)
                if frozen_edit_resets > max_frozen_edit_resets:
                    print("[run] FROZEN_EDIT: cap exceeded — agent kept editing "
                          "frozen file(s) after revert + relocate feedback. "
                          "Failing task.", flush=True)
                    end_reason = "FROZEN_EDIT"
                    break
                revert_okay = _revert_frozen_files(frozen_changed)
                # The revert is best-effort (added/renamed paths aren't restored
                # by `checkout HEAD`); confirm the frozen tree is actually clean
                # again. If any frozen change survives, we must NOT pretend it
                # was reverted — fail terminally so the post-loop gate keeps
                # FROZEN_EDIT and no false green can slip through (P2).
                residual_frozen = _frozen_files_changed()
                if frozen_check_error:
                    print(f"[run] FROZEN_EDIT: post-revert frozen-file audit "
                          f"failed ({frozen_check_error}) — failing task.",
                          flush=True)
                    end_reason = "FROZEN_EDIT"
                    break
                if (not revert_okay) or residual_frozen:
                    print(f"[run] FROZEN_EDIT: revert incomplete "
                          f"(okay={revert_okay}, residual={residual_frozen}) "
                          "— failing task.", flush=True)
                    end_reason = "FROZEN_EDIT"
                    break
                # Taint this round: `verus_okay` was computed BEFORE the revert,
                # while the now-removed frozen helper/proof was present, so it no
                # longer describes the worktree. round_results[-1] feeds the
                # final-state gate's `last_round_okay`; if this frozen-recovery
                # round is the last one (budget/rounds exhausted), an un-tainted
                # True would promote to a COMPLETE the post-revert state never
                # earned. Mark it not-okay so COMPLETE requires a clean,
                # re-verified post-revert round (P1).
                rr.verus_okay = False
                rr.end_reason = "FROZEN_EDIT_RECOVERED"
                write_json(tdir / f"round_{round_num}.json", rr)
                next_continue_msg = _frozen_edit_continue_msg(frozen_changed)
                continue
        new_axioms = _axiom_names() - baseline_axioms
        if new_axioms:
            # Hard fail: agent introduced a new `proof fn axiom_*`. Admits
            # inside it are silently excluded from the COMPLETE count, so
            # this bypasses the anti-admit gate. Same class of cheat as
            # spec drift — break and record it.
            print(f"[run] AXIOM_DRIFT: agent introduced new axiom declaration(s) "
                  f"{sorted(new_axioms)} — admits inside axiom_* bodies are "
                  f"excluded from the COMPLETE count, so this is a fake-green "
                  f"vector. Failing round.", flush=True)
            end_reason = "AXIOM_DRIFT"
            break
        introduced_forbidden = _forbidden_introduced()
        if introduced_forbidden:
            # Hard fail: agent introduced a proof-bypass construct (a new
            # `assume(...)` or `#[verifier::external_body]`). Each discharges a
            # proof obligation without SMT and leaves no admit()/axiom_* trace,
            # so the COMPLETE counters can't see it — same fake-green class as
            # the drifts above. Break and record (non-promotable end_reason).
            print(f"[run] FORBIDDEN_CONSTRUCT: agent introduced proof-bypass "
                  f"construct(s) {introduced_forbidden} — assume(...) / "
                  f"#[verifier::external_body] discharge obligations without an "
                  f"SMT proof and leave no admit()/axiom_* trace. Failing round.",
                  flush=True)
            end_reason = "FORBIDDEN_CONSTRUCT"
            break
        # If this round ended with the full checked state verifying *and* every
        # integrity gate above accepted it, mark it as the rollback target in
        # case a future round runs out of budget while leaving files broken.
        # Recovered GIT_RECOVERY/FROZEN_EDIT rounds continue before this point,
        # so a tainted pre-rollback/pre-revert green can never become a "last
        # good" snapshot.
        if rr.verus_okay:
            last_good_snapshot_round = round_num
        if sibling_fail:
            # The agent broke a sibling helper (or a top-level module that
            # consumes it). The per-round verus check covers only the TARGET
            # module, so a target-only COMPLETE here would be a false green.
            # Checked after the cheat-class gates (TOOLING/SPEC/AXIOM drift),
            # whose doctored-tooling / weakened-spec signals would make this
            # re-verify itself untrustworthy. Unlike those cheat-class gates,
            # a sibling compile/proof failure is actionable, so keep the agent
            # alive while rounds/budget remain. The round JSON now carries the
            # sibling diagnostics into the next continuation prompt; final
            # state below preserves SIBLING_VERUS_FAIL if the pending failure
            # never clears.
            next_continue_msg = _sibling_failure_continue_msg(
                sorted(pending_sibling_fail))
        if reason == "FALSE_CONTRACT":
            # E7: the agent claims a frozen contract is false. VERIFY each
            # counterexample witness against the frozen snapshot — never trust
            # the label (crying false-contract to bail is the same incentive
            # class as a fake green). Verified ⇒ FALSE_CONTRACT (terminal,
            # unclosable); none verified ⇒ unconfirmed escalation → NEEDS_DECOMP.
            fc_verified, fc_unconfirmed = _verify_false_contract_claims(
                tdir, project, spec_snapshot, experiment_allow_edit)
            false_contracts = fc_verified
            unconfirmed_false = fc_unconfirmed
            end_reason = _finalize_false_contract_round(tdir, rr, fc_verified)
            if fc_verified:
                print(f"[run] FALSE_CONTRACT: {len(fc_verified)} verified-false "
                      f"contract(s) {[v['function'] for v in fc_verified]}; "
                      f"{len(fc_unconfirmed)} unconfirmed.", flush=True)
                break
            print(f"[run] FALSE_CONTRACT escalation but 0 machine-verified "
                  f"({len(fc_unconfirmed)} unconfirmed) — downgrading to "
                  f"NEEDS_DECOMP.", flush=True)
            break
        if reason == "NEEDS_DECOMP":
            # Feature2: the agent escalated — the proof needs missing
            # infrastructure. The whole point of the escalation is to stop
            # grinding the session to the time limit, so break now and record
            # the label. A fresh run_task (e.g. a run_layer re-run) will detect
            # the NEEDS_DECOMP record and retry with a bumped budget + a
            # build-infrastructure-first directive (see top of run_task).
            end_reason = "NEEDS_DECOMP"
            break
        admits_left = _count_gate_admits(target, experiment_allow_edit)
        if reason == "COMPLETE" and rr.verus_okay and admits_left == 0:
            end_reason = "COMPLETE"
            break
        if reason == "COMPLETE" and (not rr.verus_okay or admits_left > 0):
            # Agent claimed done but evidence disagrees. Treat as LIMIT and
            # continue, and tell the agent WHY on the next round so it
            # doesn't just retry the same self-declared COMPLETE.
            print(f"[run] agent claimed COMPLETE but verus_okay={rr.verus_okay} "
                  f"admits_left={admits_left} — continuing", flush=True)
            reason = None
            next_continue_msg = _rejection_continue_msg(rr.verus_okay, admits_left)
        else:
            next_continue_msg = "continue"
        if pending_sibling_fail:
            sibling_msg = _sibling_failure_continue_msg(
                sorted(pending_sibling_fail))
            next_continue_msg = (
                sibling_msg if next_continue_msg == "continue"
                else next_continue_msg + "\n\n" + sibling_msg
            )
        # Otherwise: LIMIT or None → next round continues the session.

        # Plateau stop (P2): applied AFTER the integrity gates above, so a cheat
        # on the plateau round keeps its real end_reason. Only stop if no gate
        # already set a terminal reason this round.
        if plateau_stop_now and end_reason is None:
            print(f"[run] PLATEAU_STOP: stopping after {rounds_since_new_low} "
                  f"no-progress rounds ({metric_label} best={metric_best}).",
                  flush=True)
            end_reason = "LIMIT"
            break

    duration_total = (datetime.now() - start).total_seconds()

    # Final state — verus must pass AND no admit() may remain UNLESS
    # remaining admits are documented as intentional axioms (M4 metric).
    # `admit()` makes Verus accept any postcondition trivially, so
    # verus_okay alone is not sufficient evidence of "done."
    try:
        admits_remaining = _count_gate_admits(target, experiment_allow_edit)
    except OSError:
        admits_remaining = -1
    last_round_okay = _complete_verus_gate_okay(
        round_results[-1] if round_results else None)

    # Classify remaining admits: intentional axioms vs hard tail.
    admit_classification = classify_remaining_admits(target, experiment_allow_edit)
    intentional_axioms = admit_classification["intentional"]
    hard_remaining = admit_classification["hard"]

    # Axiom-integrity: any agent-introduced `proof fn axiom_*` (vs the
    # pre-run baseline) is a fake-green vector — fold it into the loop
    # end_reason so the final gate can't promote it to COMPLETE even if
    # the loop ended without the per-round check firing (e.g. budget bail).
    final_new_axioms = _axiom_names() - baseline_axioms
    if final_new_axioms and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "PROCESS_CROSSTALK"):
        end_reason = "AXIOM_DRIFT"

    # Tooling-integrity: any agent edit to the harness's own verification
    # skills (vs the pre-run baseline) is a fake-green vector — fold it into
    # the loop end_reason so a budget-bail / deadline exit that never reached
    # the per-round decision block still can't be promoted to COMPLETE. Does
    # not clobber an already-recorded spec/axiom cheat label.
    final_changed_tooling = _tooling_changed()
    if final_changed_tooling and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "PROCESS_CROSSTALK", "AXIOM_DRIFT"):
        end_reason = "TOOLING_DRIFT"

    # Frozen-file integrity (bridge-specs rung): any edit outside the bridge
    # module weakens a pinning consumer — fold into end_reason so a budget-bail
    # exit can't be promoted to COMPLETE off a tampered frozen file.
    final_frozen_changed = (
        _frozen_files_changed() if experiment_mode in _WHOLE_CRATE_MODES else [])
    final_frozen_check_error = (
        frozen_check_error if experiment_mode in _WHOLE_CRATE_MODES else None)
    if final_frozen_check_error and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "PROCESS_CROSSTALK", "AXIOM_DRIFT", "TOOLING_DRIFT"):
        print(f"[run] FROZEN_EDIT: final frozen-file audit failed "
              f"({final_frozen_check_error}) — cannot prove frozen files "
              "stayed unchanged. Failing task.", flush=True)
        end_reason = "FROZEN_EDIT"
    if final_frozen_changed and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "PROCESS_CROSSTALK", "AXIOM_DRIFT", "TOOLING_DRIFT"):
        end_reason = "FROZEN_EDIT"

    # Forbidden-construct integrity: any agent-introduced `assume(...)` /
    # `#[verifier::external_body]` (vs the pre-run baseline) is a fake-green
    # vector — fold into end_reason so a budget-bail / deadline exit that never
    # reached the per-round decision block still can't be promoted to COMPLETE.
    # Does not clobber an already-recorded cheat label.
    final_forbidden = _forbidden_introduced()
    if final_forbidden and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "PROCESS_CROSSTALK", "AXIOM_DRIFT", "TOOLING_DRIFT",
            "FROZEN_EDIT"):
        end_reason = "FORBIDDEN_CONSTRUCT"

    # Sibling failures are not proof-bypass cheats, so they should not kill a
    # run mid-loop. They still mean the full checked state is not done; if the
    # task exhausts rounds/budget with a sibling/top-level failure pending,
    # preserve that label instead of flattening it to LIMIT.
    if pending_sibling_fail and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT", "FROZEN_EDIT",
            "FORBIDDEN_CONSTRUCT", "GIT_RECOVERY", "PROCESS_CROSSTALK",
            "FALSE_CONTRACT", "RATE_LIMITED", "RETRY_EXHAUSTED",
            "TRANSPORT_ERROR", "USER_INTERRUPTED", "INTERRUPTED_SIGNAL"):
        end_reason = "SIBLING_VERUS_FAIL"

    # Success criterion: verus okay AND no LLM-target admit remains, with no
    # integrity cheat. Key on `admits_remaining` (== _count_gate_admits, the
    # SAME strict counter the per-round COMPLETE gate uses) as the single
    # source of truth — NOT classify_remaining_admits["hard"], whose extra
    # heuristics (axioms.rs filename, "Axiom:" docstring) can mis-flag a real
    # `lemma_*` obligation as intentional and promote a never-proved module to
    # COMPLETE (false green). classify_remaining_admits is kept for the
    # result.json detail only. (admits_remaining == -1 on a read error ⇒ not
    # done, which is the safe direction.)
    done_for_real = (
        last_round_okay and admits_remaining == 0
        and not final_new_axioms and not final_changed_tooling
        and not final_frozen_changed and not final_frozen_check_error
        and not final_forbidden
        and not pending_sibling_fail
    )

    # Final-state gate (pure decision in `_final_end_reason`, unit-tested):
    # Infrastructure halts (rate limit / retry exhaustion / transport error)
    # are preserved above all; else done_for_real ⇒ COMPLETE; else
    # NEEDS_DECOMP is preserved (Feature2); else LIMIT.
    final_end_reason = _final_end_reason(done_for_real, end_reason)

    success = final_end_reason == "COMPLETE"
    if admits_remaining > 0:
        kind = "all intentional" if hard_remaining == 0 else f"{hard_remaining} hard + {intentional_axioms} intentional"
        print(f"[info] Final state: end_reason={final_end_reason} "
              f"admits_remaining={admits_remaining} ({kind})")

    task_result = TaskResult(
        task_id=target_id, run_id=run_id,
        target_path=str(target), module_path=module,
        success=success, end_reason=final_end_reason,
        rounds_used=len(round_results),
        duration_seconds=duration_total,
        round_results=round_results,
        reset_round_starts=reset_round_starts,
        admit_classification=admit_classification,
        final_verus_okay=last_round_okay,
        final_admits_remaining=admits_remaining,
        final_hard_admits_remaining=hard_remaining,
        final_intentional_axiom_admits=intentional_axioms,
        final_error_count=(
            round_results[-1].verus_error_count_raw
            if round_results else 0),
        final_spec_drift_count=(
            len(round_results[-1].spec_drift) if round_results else 0),
        experiment_provenance=experiment_provenance,
    )
    write_json(tdir / "result.json", task_result)

    # E7 telemetry: machine-verified false contracts (and unconfirmed escalations)
    # as a side file, so "k hard admits" can be split into verified-false vs hard
    # without churning the TaskResult schema.
    if false_contracts or unconfirmed_false:
        write_json(tdir / "false_contracts.json",
                   {"verified_false": false_contracts,
                    "unconfirmed_false": unconfirmed_false})
        print(f"[info] false contracts: {len(false_contracts)} verified-false, "
              f"{len(unconfirmed_false)} unconfirmed → {tdir}/false_contracts.json")
    persist_retry_memory = _should_persist_retry_memory(final_end_reason)

    # Feature 3: mine this run's trace into a discovery brief for the next
    # attempt on this target (persisted whether or not it succeeded).
    if persist_retry_memory:
        try:
            # In experiment mode the editable set is authoritative: edits to any
            # other (frozen) file must not be re-recommended as "start here" — that
            # is a self-reinforcing FROZEN_EDIT loop. Relativize the allow-edit
            # paths to the project so they match the brief's project-relative keys.
            editable_rel: Optional[set[str]] = None
            if experiment_edit_scope:
                editable_rel = set()
                for _p in experiment_edit_scope:
                    try:
                        editable_rel.add(
                            str(_p.resolve().relative_to(project.resolve())))
                    except (ValueError, OSError):
                        editable_rel.add(str(_p))
            discovery_brief.update(results_root, target_id, tdir, project,
                                   editable=editable_rel)
        except Exception as e:  # never let brief-mining break a run
            print(f"[run] Feature3: discovery_brief.update failed: {e!r}", flush=True)
    else:
        print(f"[run] Feature3: discovery_brief SKIPPED "
              f"({final_end_reason} trace is retry-memory tainted)", flush=True)

    # Record to failure memory on non-success
    if not success and persist_retry_memory:
        # Feature 1: resolve the rejected decls to fn bodies. The resolved
        # names are the real fns (line-matched on current Verus, since the
        # parsed failed_declarations are unreliable), so store those rather
        # than the raw parse — which on current Verus is the crate-name junk
        # the name regex captures, never an actual fn.
        nm_names, nm_source = _extract_near_miss(
            target, last_failed_decls, last_failed_locs)
        failure_memory.record(
            results_root=results_root, run_id=run_id,
            module=module, function=target_id,
            rounds_used=len(round_results),
            final_error=last_verus_err,
            end_reason=final_end_reason,
            failed_decls=nm_names,                                 # Feature 1
            near_miss=nm_source,                                   # Feature 1
        )
    elif not success:
        print(f"[run] failure_memory: SKIPPED "
              f"({final_end_reason} trace is retry-memory tainted)", flush=True)

    # Append to proven registry on success: we record the target file stem,
    # not individual fns (MVP has no per-fn tracking yet; that's extension E3).
    if success:
        reg_path = results_root / "proven_registry.json"
        existing = json.loads(reg_path.read_text()) if reg_path.exists() else {"proven": []}
        existing["proven"].append({
            "name": target_id,
            "module": module,
            "file": str(target.relative_to(project)) if target.is_relative_to(project) else str(target),
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        results_root.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(existing, indent=2))

    # Emit diff.md — admitted vs final vs ground-truth
    if admitted_ref:
        diff_path = tdir / "diff.md"
        rc_diff, _, stderr_diff = run_subskill(
            [sys.executable, str(HERE / "skills" / "diff_view.py"),
             str(target),
             "--admitted-ref", admitted_ref,
             "--truth-ref", truth_ref,
             "--out", str(diff_path)],
            env=env,
        )
        if rc_diff == 0:
            print(f"[info] diff written to {diff_path}")
        else:
            print(f"[warn] diff_view failed (rc={rc_diff}): {stderr_diff[:500]}")

    _print_summary(task_result)
    return task_result


def _print_summary(result: TaskResult) -> None:
    print("\n" + "=" * 60)
    print(f"Task: {result.task_id}")
    print(f"Status: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"End reason: {result.end_reason}")
    print(f"Rounds: {result.rounds_used}")
    print(f"Duration: {result.duration_seconds:.1f}s")
    if result.round_results:
        last = result.round_results[-1]
        print(f"Final verus_okay: {last.verus_okay}")
        print(f"Final error count: {last.verus_error_count_raw}")
    print("=" * 60)


# ----------------- CLI -----------------

def main() -> int:
    _install_signal_handler()
    ap = argparse.ArgumentParser()
    ap.add_argument("target", type=Path, help="Target .rs file (must live inside a Cargo project)")
    ap.add_argument("--project", type=Path, default=None,
                    help="Cargo project root (auto-detected from target if omitted)")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--model", default=None,
                    help="Claude model (alias 'sonnet'/'opus'/'haiku' or full id). "
                         "Default: whatever claude-code is configured to use.")
    ap.add_argument("--vstd-root", type=Path, default=None,
                    help="Path to Verus's vstd source to index alongside the "
                         "project. Example: /path/to/verus/vstd")
    ap.add_argument("--admitted-ref", default=None,
                    help="Git ref of the admitted baseline (e.g. "
                         "eval/admitted-layerA-debug). Enables diff.md generation.")
    ap.add_argument("--truth-ref", default="main",
                    help="Git ref of the ground-truth version for the diff. "
                         "Default: main")
    ap.add_argument("--max-task-minutes", type=float, default=None,
                    help="Wall-clock cap in minutes. SIGKILL the claude "
                         "process group if exceeded. If omitted, the budget "
                         "scales with the number of admit() in the target "
                         "file: max(20, 1.5 * num_admits). Empirically derived "
                         "from Layer A/B/C runs across 29 modules.")
    ap.add_argument("--budget-min-floor", type=float, default=20.0,
                    help="Minimum auto-budget when --max-task-minutes is "
                         "omitted. Default: 20 min.")
    ap.add_argument("--budget-min-per-admit", type=float, default=1.5,
                    help="Minutes per admit() for auto-budget. Default: 1.5.")
    ap.add_argument("--no-failure-memory", action="store_true",
                    help="Skip rendering prior failure_memory records into "
                         "the prompt for this run. Useful when prior records "
                         "predate prompt/harness improvements and would prime "
                         "the agent to give up.")
    ap.add_argument("--verus-rlimit", type=float, default=_DEFAULT_VERUS_RLIMIT,
                    help="Pass --rlimit FLOAT to all harness-level "
                         "verus_check invocations. Increases the per-fn SMT "
                         "resource limit. Default 80 (Verus's own default "
                         "is ~10, which empirically rlimits out on any "
                         "non-trivial exec function — ristretto.rs's "
                         "compress, double_and_compress_batch_verus, etc.).")
    ap.add_argument("--auto-reset", dest="auto_reset", action="store_true", default=True,
                    help="Auto-reset claude session on stall or context "
                         "bloat. Default: on.")
    ap.add_argument("--no-auto-reset", dest="auto_reset", action="store_false",
                    help="Disable auto-reset (keep -c continuation throughout).")
    ap.add_argument("--max-auto-resets", type=int, default=3,
                    help="Cap on auto-resets per task. Default: 3.")
    ap.add_argument("--max-git-recovery-resets", type=int, default=3,
                    help="A git-history peek discards the round + resets the "
                         "session instead of failing the task; this caps how "
                         "many times before a repeat offender fails with "
                         "GIT_RECOVERY. Default: 3.")
    ap.add_argument("--max-frozen-edit-resets", type=int, default=3,
                    help="A frozen-file edit (whole-crate experiment modes) is "
                         "reverted to baseline + the agent is asked to relocate "
                         "the work into an editable file, instead of failing "
                         "the task; this caps how many times before a repeat "
                         "offender fails with FROZEN_EDIT. Default: 3.")
    ap.add_argument("--stall-max-duration-sec", type=float, default=180.0,
                    help="Round shorter than this counts toward stall "
                         "detection. Default: 180 (3 min).")
    ap.add_argument("--bloat-threshold-tokens", type=int, default=200_000,
                    help="Cumulative cache_creation tokens per session past "
                         "this triggers preemptive reset. Default: 200000 "
                         "(lowered from 300000: context degradation is the "
                         "dominant failure mode, so shed the session sooner "
                         "and resume with compact round-history feedback).")
    ap.add_argument("--plateau-stop-rounds", type=int, default=6,
                    help="Stop the run after this many rounds with no NEW low in "
                         "gate-scope admits (a fresh-session 'work the listed "
                         "admits' directive fires at half this). Duration-"
                         "independent, unlike the stall detector. 0 disables. "
                         "Default: 6.")
    ap.add_argument("--no-spec-gate", action="store_true",
                    help="Skip the in-loop spec_check verify (snapshot still "
                         "taken for post-hoc diff). Used by the spec-"
                         "reconstruction experiment, where the agent is "
                         "expected to ADD specs back to dependency files.")
    ap.add_argument("--experiment-allow-edit", type=Path, nargs="+", default=None,
                    help="Dependency file(s) the agent may edit. Renders an "
                         "experiment-mode block into the prompt that "
                         "overrides rule 4 (edit only target) for these "
                         "files. Required for --experiment-mode.")
    ap.add_argument("--experiment-active-edit", type=Path, nargs="+", default=None,
                    help="Optional stricter active edit scope for this run. "
                         "Files must be a subset of --experiment-allow-edit. "
                         "The full allow-edit set is still peeled/snapshotted "
                         "for oracle hygiene and admit accounting, but the "
                         "prompt and frozen-file guard allow edits only here.")
    ap.add_argument("--experiment-mode",
                    choices=["spec-proof", "proof-only", "contract-only",
                             "bridge-specs", "bridge-full", "field-floor"],
                    default="spec-proof",
                    help="Which experiment shape to run. "
                         "'spec-proof' (default): dep fns have no Verus "
                         "specs; agent infers requires/ensures/decreases AND "
                         "adds proof scaffolding; helper lemmas allowed; "
                         "in-loop spec-integrity gate disabled. "
                         "'proof-only': specs and lemma library are frozen; "
                         "agent only adds proof scaffolding inside existing "
                         "fn bodies; spec-integrity gate stays ON so any "
                         "fn-header edit fails the round. "
                         "'contract-only': the anchor's contract is frozen "
                         "(gate ON) but its proof body was stripped and its "
                         "helper lemmas deleted; agent rewrites the anchor's "
                         "proof body AND invents the lemmas. The anchor file "
                         "is editable, but its contract clauses stay frozen. "
                         "'bridge-specs': two shared `open spec fn`s (the "
                         "Montgomery<->Edwards map in decompress_bridge_specs.rs) "
                         "are DELETED; agent reconstructs them so the WHOLE "
                         "crate verifies. Soundness comes from frozen consumers "
                         "(montgomery::to_edwards's proof) that pin the map: "
                         "whole-crate verify each round + a changed-files guard "
                         "so only the bridge module may be edited. "
                         "'bridge-full': bridge-specs PLUS the decompress lemma "
                         "chain — the entry-point lemmas a frozen consumer calls "
                         "keep their requires/ensures (gate-frozen) but lose "
                         "their bodies, the internal helpers are deleted, and "
                         "the lemma file joins the editable set. Agent rebuilds "
                         "the map defs, both entry-lemma proofs, and the internal "
                         "helpers. Same whole-crate verify + frozen-file guard.")
    ap.add_argument("--wire-log", action="store_true",
                    help="Route the claude subprocess through a localhost "
                         "logging proxy (wire_proxy.py) via ANTHROPIC_BASE_URL, "
                         "capturing the full API request bodies into "
                         "claude_raw/wire_*.jsonl: system prompt, tool JSON "
                         "schemas, skills, and per-turn context growth — none "
                         "of which appear in the stream-json logs. Best-effort: "
                         "a proxy failure falls back to the direct API and "
                         "never fails the run. Off by default.")
    ap.add_argument("--sibling-verify", dest="sibling_verify",
                    action="store_true", default=True,
                    help="After each round, re-verify any sibling helper the "
                         "agent modified plus its area's top-level module. "
                         "Catches sibling edits that break transitive "
                         "verification. Default: on.")
    ap.add_argument("--no-sibling-verify", dest="sibling_verify",
                    action="store_false",
                    help="Disable the per-round sibling re-verify gate "
                         "(only the target module is checked).")
    args = ap.parse_args()
    if args.experiment_allow_edit:
        # spec-proof: agent rewrites specs, so the snapshot-vs-current gate
        # would always fail. proof-only / contract-only: gate must stay ON
        # since the contract clauses are frozen and any drift = cheating.
        if args.experiment_mode == "spec-proof":
            args.no_spec_gate = True
        # proof-only & contract-only: leave --no-spec-gate at whatever the user
        # set (default False), so the gate runs and catches contract edits.
        # contract-only is the first rung where the ANCHOR file itself is
        # editable, yet spec_check only freezes contract clauses (header +
        # requires/ensures/decreases), not bodies — so the agent can rewrite
        # the anchor's proof body while a weakened ensures still trips the gate.
    if args.experiment_active_edit:
        if not args.experiment_allow_edit:
            print("[error] --experiment-active-edit requires --experiment-allow-edit",
                  file=sys.stderr)
            return 2
        allow_set = {p.resolve() for p in args.experiment_allow_edit}
        active_set = {p.resolve() for p in args.experiment_active_edit}
        bad = sorted(str(p) for p in active_set - allow_set)
        if bad:
            print("[error] --experiment-active-edit must be a subset of "
                  f"--experiment-allow-edit; extra={bad}", file=sys.stderr)
            return 2

    target = args.target.resolve()
    if not target.exists():
        print(f"[error] target not found: {target}", file=sys.stderr)
        return 1
    project = (args.project or find_cargo_root(target)).resolve()
    run_id = args.run_id or results.run_id_new()
    results_root = args.results.resolve()
    results_root.mkdir(parents=True, exist_ok=True)

    # Budget: explicit override, or auto-scale by admit count.
    if args.max_task_minutes is not None:
        max_minutes = args.max_task_minutes
        budget_source = "explicit"
    else:
        try:
            # Count NON-AXIOM admits only — the same axiom-aware counter the
            # COMPLETE gate uses (_count_gate_admits / _admit_count). A raw
            # .count("admit()") over-counts: it includes admit()s inside
            # `proof fn axiom_*` bodies (allowed to stay) and any in
            # comments/strings, inflating the auto budget.
            num_admits = _count_llm_target_admits(target.read_text())
        except OSError:
            num_admits = 0
        auto = max(args.budget_min_floor, args.budget_min_per_admit * num_admits)
        max_minutes = auto
        budget_source = (
            f"auto (max({args.budget_min_floor}, "
            f"{args.budget_min_per_admit} * {num_admits} admits) = {auto:.0f})"
        )

    print(f"[run] target   = {target}")
    print(f"[run] project  = {project}")
    print(f"[run] run_id   = {run_id}")
    print(f"[run] results  = {results_root}")
    print(f"[run] rounds   = {args.rounds}")
    print(f"[run] budget   = {max_minutes:.1f} min  ({budget_source})")
    print(f"[run] pid      = {os.getpid()}  (Ctrl-C or kill -TERM {os.getpid()} to stop)")

    result = run_task(
        target=target, project=project,
        run_id=run_id, results_root=results_root,
        max_rounds=args.rounds,
        model=args.model,
        vstd_root=args.vstd_root.resolve() if args.vstd_root else None,
        admitted_ref=args.admitted_ref,
        truth_ref=args.truth_ref,
        max_task_minutes=max_minutes,
        skip_failure_memory=args.no_failure_memory,
        verus_rlimit=args.verus_rlimit,
        auto_reset=args.auto_reset,
        max_auto_resets=args.max_auto_resets,
        max_git_recovery_resets=args.max_git_recovery_resets,
        max_frozen_edit_resets=args.max_frozen_edit_resets,
        stall_max_duration_sec=args.stall_max_duration_sec,
        bloat_threshold_tokens=args.bloat_threshold_tokens,
        plateau_stop_rounds=args.plateau_stop_rounds,
        experiment_allow_edit=[p.resolve() for p in (args.experiment_allow_edit or [])],
        experiment_active_edit=[p.resolve() for p in (args.experiment_active_edit or [])],
        experiment_mode=args.experiment_mode,
        no_spec_gate=args.no_spec_gate,
        wire_log=args.wire_log,
        sibling_verify=args.sibling_verify,
    )
    # Distinct exit code 42 on a 429 halt so a batch launcher (launch.sh) can
    # tell "this target failed" (rc 1) from "the quota window is exhausted,
    # stop the whole sweep" (rc 42) — every later target would just be
    # rejected too until the window resets.
    if result.end_reason == "RATE_LIMITED":
        return 42
    if _RECEIVED_SIGNAL is not None:
        return 128 + _RECEIVED_SIGNAL
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
