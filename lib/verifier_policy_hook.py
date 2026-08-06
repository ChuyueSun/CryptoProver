#!/usr/bin/env python3
"""PreToolUse Bash hook: hard-block verifier commands that the prompt forbids.

Rationale (AGENT_DEBATE T-crosstalk): `prompt.md` already tells the agent to run
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
`cargo verus | grep/head` substitution codex flagged. Process wrappers are an
enumerated defence-in-depth surface, not a complete command-language security
boundary; extend the wrapper set when a new executable prefix is observed.
"""
import json
import os
import re
import shlex
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
# Verifier/skill stdout redirected to ANY file, not just shared /tmp: a
# pollable on-disk artifact reintroduces exactly what the policy text forbids
# (backgrounded verifier + file-polling + parse-when-ready), as observed live
# in stage3 A6 r16 where blocking only /tmp pushed the same pattern to
# /work/wc.json. Allows stderr-only redirects (2>), /dev/null, and >&2.
_STDOUT_FILE_REDIRECT = re.compile(
    r"(?:&>>?|(?<![\w&])1>{1,2}(?!&)|(?<![0-9&])>{1,2}(?!&))\s*(?!/dev/null\b)\S+"
)
_HARNESS_SKILL = re.compile(
    r"\b(?:verus_check|spec_check|admit_inventory|search_semantic|search_module|"
    r"search_macro|search_proven)\.py\b"
)
_VERIFIER_OUTPUT_SLICE = re.compile(r"\|\s*(?:head|tail|grep)\b")
# Any pipe makes verifier stdout a secondary protocol and encourages sliced or
# delayed parsing. `||` remains ordinary shell control flow, not a pipeline.
_VERIFIER_PIPE = re.compile(r"\|&|(?<!\|)\|(?!\|)")
_VERUS_CHECK_HELP = re.compile(r"\bverus_check\.py\b[\s\S]*?(?:^|\s)(?:-h|--help)(?:\s|$)")
_RAW_ADMIT_COUNT_FLAG = re.compile(r"\b(?:grep|rg)\b[\s\S]*\s(?:-[A-Za-z]*c[A-Za-z]*|--count)\b")
_RAW_ADMIT_COUNT_PIPE = re.compile(r"\|\s*wc\s+-l\b")

_VERUS_EXEC_WRAPPERS = {
    "nice", "stdbuf", "xargs", "sudo", "setsid", "chrt", "ionice",
    "taskset", "time", "script",
}
_PYTHON_PROCESS_CALL = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.system)\s*\("
)
_PYTHON_VERIFIER_ARG = re.compile(
    r"(?:['\"]verus['\"]|['\"]cargo['\"][\s\S]{0,120}['\"]verus['\"]|"
    r"\bcargo\s+verus\b)"
)
_MAX_COMMAND_BYTES = 128 * 1024


class _Fd2Token(str):
    """Out-of-band token marking a lexically adjacent fd-2 redirect."""


def _is_shell_command_option(token):
    """True only for short shell option words carrying the `-c` flag."""
    return bool(
        token.startswith("-") and not token.startswith("--")
        and "c" in token[1:]
    )


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


def _shell_tokens(cmd):
    """Tokenize shell operators without treating quoted query text as syntax."""
    try:
        source = _strip_shell_comments(cmd)
        # Preserve adjacent fd-2 redirects (`2>file`) distinctly from a literal
        # argument followed by stdout redirection (`--rlimit 2 > file`). shlex
        # otherwise flattens both into the same token sequence. Choose a marker
        # absent from the command, then convert it to a typed token immediately
        # after lexing. NUL cannot occur in a command accepted by execve; if it
        # arrives in tool JSON anyway, fail closed rather than treating it as a
        # shell word. Quote concatenation therefore cannot forge this identity.
        if "\0" in source or "\x01" in source:
            return []
        fd2_marker = "\0"
        newline_marker = "\x01"
        source = _mark_unquoted_newlines(source, newline_marker)
        source = re.sub(r"(?<!\S)2(?=>)", fd2_marker, source)
        lexer = shlex.shlex(
            source,
            posix=True,
            punctuation_chars="|&;<>",
        )
        lexer.commenters = ""
        lexer.wordchars += fd2_marker + newline_marker
        raw_tokens = [
            _Fd2Token("2") if token == fd2_marker
            else ";" if token == newline_marker
            else token
            for token in lexer
        ]
        tokens = []
        index = 0
        while index < len(raw_tokens):
            if (raw_tokens[index:index + 2] == ["$", "{"]
                    and index + 3 < len(raw_tokens)
                    and re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]*", raw_tokens[index + 2],
                    )
                    and raw_tokens[index + 3] == "}"):
                tokens.append("$" + "{" + raw_tokens[index + 2] + "}")
                index += 4
                continue
            if (raw_tokens[index] == "$" and index + 1 < len(raw_tokens)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw_tokens[index + 1])):
                tokens.append("$" + raw_tokens[index + 1])
                index += 2
                continue
            tokens.append(raw_tokens[index])
            index += 1
    except ValueError:
        # An incomplete quote is malformed shell anyway. Retain the regex
        # backstop instead of allowing it through because tokenization failed.
        return []
    expanded = list(tokens)
    for token in tokens:
        cursor = 0
        while True:
            start = token.find("$(", cursor)
            if start < 0:
                break
            depth = 1
            index = start + 2
            while index < len(token) and depth:
                if token.startswith("$(", index):
                    depth += 1
                    index += 2
                    continue
                if token[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            if depth:
                # Malformed shell. Force the regex fallback used by evaluate.
                return []
            expanded.extend(_shell_tokens(token[start + 2:index]))
            cursor = index + 1
    # Shell -c payloads are a nested command language. Expand them into the
    # analysis stream so verifier pipes/redirects inside a wrapper receive the
    # same policy as direct commands (conservative over-attribution is safer).
    for index, token in enumerate(tokens[:-2]):
        if os.path.basename(token) not in {"sh", "bash", "zsh"}:
            continue
        for option_index in range(index + 1, len(tokens) - 1):
            if _is_shell_command_option(tokens[option_index]):
                expanded.extend(_shell_tokens(tokens[option_index + 1]))
                break
    return expanded


def _strip_shell_comments(cmd):
    """Remove real Bash comments while retaining `#` inside a shell word."""
    out = []
    quote = None
    escaped = False
    index = 0
    while index < len(cmd):
        char = cmd[index]
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            out.append(char)
            escaped = True
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            out.append(char)
            quote = char
        elif char == "#" and (
            index == 0 or cmd[index - 1].isspace()
            or cmd[index - 1] in ";|&()"
        ):
            newline = cmd.find("\n", index)
            if newline < 0:
                break
            out.append("\n")
            index = newline
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _mark_unquoted_newlines(source, marker):
    """Replace shell command newlines with a lexer-visible separator marker."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    quote = None
    escaped = False
    for char in source:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            out.append(char)
            escaped = True
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            out.append(char)
            quote = char
        elif char == "\n":
            out.extend((" ", marker, " "))
        else:
            out.append(char)
    return "".join(out)


def _stdout_file_redirect(tokens):
    for index, token in enumerate(tokens):
        if token not in {">", ">>", ">|", ">&", "&>", "&>>"}:
            continue
        if (token in {">", ">>", ">|", ">&"} and index
                and isinstance(tokens[index - 1], _Fd2Token)):
            continue
        destination = tokens[index + 1] if index + 1 < len(tokens) else ""
        if destination == "/dev/null" or (token == ">&" and destination == "2"):
            continue
        return True
    return False


def _verifier_pipe(tokens):
    return any(token in {"|", "|&"} for token in tokens)


def _verifier_output_slice(tokens):
    for index, token in enumerate(tokens[:-1]):
        if token in {"|", "|&"} and os.path.basename(tokens[index + 1]) in {
            "head", "tail", "grep",
        }:
            return True
    return False


_CONTROL_TOKENS = {";", "&&", "||", "&"}


def _token_segments(tokens):
    """Group a token stream into control segments (split at `;`/`&&`/`||`/`&`).

    Pipes bind WITHIN a segment: output-shape rules (pipe/slice/redirect)
    apply only to segments that actually invoke a verifier or harness skill,
    so a pipe or redirect in an unrelated segment of a compound command is
    ordinary shell, not verifier-output handling (F4). Tokens expanded out of
    a command substitution are appended to the trailing segment, which can
    over-attribute a substitution's pipe to a later verifier segment — a
    conservative false positive the corrective message resolves.
    """
    segments, current = [], []
    for token in tokens:
        if token in _CONTROL_TOKENS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _direct_verus_from_tokens(tokens, _depth=0, _inherited_assignments=None):
    """Recognize direct Verus after ordinary shell wrappers/assignments."""
    if _depth >= 16:
        # Nested command-language wrappers are untrusted input. Fail closed
        # before Python recursion exhaustion can turn the hook into an error
        # path that Claude Code may treat differently from an explicit deny.
        return True
    assignments = {
        name: set(values)
        for name, values in (_inherited_assignments or {}).items()
    }
    for token in tokens:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.+)", token)
        if match:
            assignments.setdefault(match.group(1), set()).add(match.group(2))
    command_separators = _CONTROL_TOKENS | {"|", "|&"}
    commands, current = [], []
    for token in tokens:
        if token in command_separators:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    for words in commands:
        index = 0
        while index < len(words) and (
                words[index] in {"{", "}", "(", ")"}
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index])):
            index += 1
        while index < len(words):
            word = os.path.basename(words[index])
            if word in {"command", "exec", "nohup", "eval"}:
                index += 1
                continue
            if word in _VERUS_EXEC_WRAPPERS:
                # These process-launch wrappers have heterogeneous option
                # grammars. Search only their remaining argv for the exact
                # verifier executable instead of guessing where options end.
                if any(os.path.basename(arg) == "verus"
                       for arg in words[index + 1:]):
                    return True
                if word == "script":
                    for option_index in range(index + 1, len(words)):
                        option = words[option_index]
                        if option in {"-c", "--command"}:
                            if option_index + 1 < len(words):
                                nested = _shell_tokens(words[option_index + 1])
                                if nested and _direct_verus_from_tokens(
                                        nested, _depth + 1, assignments):
                                    return True
                            break
                        if option.startswith("--command="):
                            nested = _shell_tokens(option.split("=", 1)[1])
                            if nested and _direct_verus_from_tokens(
                                    nested, _depth + 1, assignments):
                                return True
                            break
                index += 1
                continue
            if word == "env":
                index += 1
                while index < len(words) and (
                        words[index].startswith("-")
                        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index])):
                    index += 1
                continue
            if word == "timeout":
                index += 1
                while index < len(words) and words[index].startswith("-"):
                    index += 1
                if index < len(words):
                    index += 1  # duration
                continue
            break
        if index >= len(words):
            continue
        command = words[index]
        shell = os.path.basename(command)
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", shell):
            for option_index in range(index + 1, len(words) - 1):
                if words[option_index] != "-c":
                    continue
                payload = words[option_index + 1]
                if (_PYTHON_PROCESS_CALL.search(payload)
                        and _PYTHON_VERIFIER_ARG.search(payload)):
                    return True
                break
        if shell in {"sh", "bash", "zsh"}:
            for option_index in range(index + 1, len(words) - 1):
                option = words[option_index]
                if _is_shell_command_option(option):
                    nested = _shell_tokens(words[option_index + 1])
                    if nested and _direct_verus_from_tokens(
                            nested, _depth + 1, assignments):
                        return True
                    break
        if os.path.basename(command) == "verus":
            return True
        variable = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", command)
        if variable and any(
            os.path.basename(value) == "verus"
            for value in assignments.get(variable.group(1), ())
        ):
            return True
    return False


def _segment_matches_verifier(segment):
    segment_text = " ".join(segment)
    return bool(
        (_VERIFIER.search(segment_text) or _DIRECT_VERUS_RAW.search(segment_text)
         or _direct_verus_from_tokens(segment))
        and not _is_verus_check_help_only(segment_text)
    )

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
        for command in (
            ["git", "-C", project_root, "diff", "--quiet", "--", path],
            ["git", "-C", project_root, "diff", "--cached", "--quiet",
             "--", path],
        ):
            proc = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if proc.returncode == 1:
                return True
            if proc.returncode != 0:
                return True
        untracked = subprocess.run(
            ["git", "-C", project_root, "ls-files", "--others",
             "--exclude-standard", "--", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return True
    if untracked.returncode != 0:
        return True
    return bool(untracked.stdout.strip())


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


def _agent_timeout_exceeds_cap(tokens, raw_cmd=""):
    raw_cap = os.environ.get("DALEK_AGENT_MAX_VERIFIER_TIMEOUT", "").strip()
    if not raw_cap:
        return False
    try:
        cap = float(raw_cap)
    except ValueError:
        return False
    # Both argparse spellings count (`--timeout N` and `--timeout=N`). A value
    # that is not a numeric literal ($VAR, command substitution, quoted)
    # cannot be checked against the cap statically, so it fails closed — the
    # corrective reason tells the agent to pass a literal within the cap (F6).
    values = []
    for index, token in enumerate(tokens):
        if token == "--timeout":
            values.append(tokens[index + 1] if index + 1 < len(tokens) else "")
        elif token.startswith("--timeout="):
            values.append(token.split("=", 1)[1])
    if not tokens and "--timeout" in raw_cmd:
        values = re.findall(
            r"(?:^|\s)--timeout(?:=|\s+)([^\s;|&]+)", raw_cmd,
        )
        if not values:
            return True
    for value in values:
        try:
            if float(value) > cap:
                return True
        except ValueError:
            return True
    return False

_MSG = (
    "BLOCKED by proof-harness policy ({reasons}). Per prompt.md: run "
    "`python3 {verus_check} <target> --project <root>` in the FOREGROUND "
    "and read its JSON stdout directly. Use `verus_check.py --timeout N` (and "
    "`--rlimit N`) instead of a shell `timeout` wrapper; do NOT use "
    "run_in_background, do NOT redirect verifier output to /tmp/*.json|out|log, "
    "do NOT pipe merged stderr into a JSON parser, "
    "do NOT pipe verifier output to another command, "
    "do NOT use raw grep/rg counts for admits (use admit_inventory.py over the "
    "full editable scope), "
    "do NOT pkill/killall/pgrep -f, and never substitute raw `cargo verus` "
    "(or `cargo verus | grep/head`) for verus_check.py. The harness owns this "
    "round's timeout + cleanup; in whole-crate experiment modes the runner "
    "also owns the package gate."
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
    if len(cmd.encode("utf-8")) > _MAX_COMMAND_BYTES:
        # Bound parser work before shlex/recursive wrapper expansion. Commands
        # this large cannot be audited cheaply and are far beyond normal proof
        # harness invocations, so make the denial explicit and fail closed.
        return ["Bash command exceeds proof-harness policy parser size limit"]
    if "\0" in cmd or "\x01" in cmd:
        return ["Bash command contains unsupported policy-parser control byte"]
    bg = bool((tool_input or {}).get("run_in_background"))
    shell_tokens = _shell_tokens(cmd)
    reasons = []
    direct_verus = bool(
        _DIRECT_VERUS_RAW.search(cmd)
        or (shell_tokens and _direct_verus_from_tokens(shell_tokens))
    )
    is_verifier = bool(_VERIFIER.search(cmd) or direct_verus) and not _is_verus_check_help_only(cmd)
    is_harness_skill = bool(_HARNESS_SKILL.search(cmd))
    if _pre_edit_guard_active() and (is_verifier or is_harness_skill) and not _pre_edit_guard_has_diff():
        reasons.append("pre-edit proof-thread diagnostic before active source diff")
    if bg and is_verifier:
        reasons.append("run_in_background verifier")
    if is_verifier and shell_tokens and "&" in shell_tokens:
        reasons.append("shell-backgrounded verifier")
    if is_verifier and _TIMEOUT.search(cmd):
        reasons.append("shell timeout-wrapped verifier")
    if is_verifier and _SHARED_TMP.search(cmd):
        reasons.append("verifier output to shared /tmp/*.{json,out,log,err}")
    if (is_verifier or is_harness_skill) and _MERGED_STDERR_JSON.search(cmd):
        reasons.append("merged stderr into harness-skill JSON parser")
    if shell_tokens:
        # Scope output-shape rules to the control segments that actually
        # invoke a verifier or harness skill (F4): a pipe or redirect in an
        # unrelated segment of a compound command is plain shell.
        segments = _token_segments(shell_tokens)
        verifier_segments = [
            seg for seg in segments if _segment_matches_verifier(seg)
        ]
        guarded_segments = verifier_segments + [
            seg for seg in segments
            if _HARNESS_SKILL.search(" ".join(seg)) and seg not in verifier_segments
        ]
        redirect_hit = any(_stdout_file_redirect(seg) for seg in guarded_segments)
        inherited_redirect_hit = any(
            seg and os.path.basename(seg[0]) == "exec" and _stdout_file_redirect(seg)
            for seg in segments
        ) or (
            any(token in {"{", "("} for token in shell_tokens)
            and any(_stdout_file_redirect(seg) for seg in segments)
        )
        redirect_hit = redirect_hit or bool(verifier_segments and inherited_redirect_hit)
        slice_hit = any(_verifier_output_slice(seg) for seg in verifier_segments)
        pipe_hit = any(_verifier_pipe(seg) for seg in verifier_segments)
    else:
        # Tokenization failed (malformed shell) — regex backstop over the
        # whole command, fail-closed.
        redirect_hit = bool((is_verifier or is_harness_skill)
                            and _STDOUT_FILE_REDIRECT.search(cmd))
        slice_hit = bool(is_verifier and _VERIFIER_OUTPUT_SLICE.search(cmd))
        pipe_hit = bool(is_verifier and _VERIFIER_PIPE.search(cmd))
    if redirect_hit:
        reasons.append(
            "verifier/skill stdout redirected to a file (read JSON stdout directly)")
    if slice_hit:
        reasons.append("verifier output piped through head/tail/grep")
    if pipe_hit:
        reasons.append("verifier output piped to another command (read JSON stdout directly)")
    if is_verifier and _agent_timeout_exceeds_cap(shell_tokens, cmd):
        raw_cap = os.environ.get("DALEK_AGENT_MAX_VERIFIER_TIMEOUT", "").strip()
        reasons.append(
            "agent verifier timeout exceeds runner policy cap "
            f"(pass a literal --timeout <= {raw_cap})")
    if (is_verifier and os.environ.get("DALEK_RUNNER_OWNS_WHOLE_CRATE") == "1"
            and "--whole-crate" in cmd):
        reasons.append("whole-crate verifier is runner-owned for this experiment")
    if _is_raw_admit_count(cmd):
        reasons.append("raw admit count (use admit_inventory.py)")
    if _BROAD_PROC.search(cmd):
        reasons.append("broad process control (pkill/killall/pgrep -f)")
    if _CARGO_RAW.search(cmd):
        if _CARGO_GREP.search(cmd):
            reasons.append("raw cargo-verus | grep/head substitution (truncates errors)")
        else:
            reasons.append("raw cargo-verus substitution (use verus_check.py)")
    if direct_verus:
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
