#!/usr/bin/env python3
"""Deterministic peel-depth init-state builder (+ git worktree).

ONE axis — *peel depth* — controls how much of a proof target's content
the agent must reconstruct. Each depth removes one more shell of the
proof stack, strictly cumulative (depth N removes a prefix of the stack's
shells, so difficulty is totally ordered):

    P1  proofs    admit `proof fn` bodies + inline `proof { }` blocks
    P2  lemmas    + delete named helper lemmas (sig + contract + body)
    P3  specs     + delete named `spec fn` definitions
    P4  contract  + strip requires/ensures/decreases off named anchors
                  ── REQUIRES a pin (P1-P3 are self-pinning) ──

The FROZEN FLOOR — exec code and `axiom_*` / `assume` / `external_body`
— is never peeled. It is safe by construction: the shell ops only touch
`proof fn` bodies (axiom-skipping) and the *named* lemmas / spec fns /
anchors in the manifest, so as long as the manifest lists only
proof-layer items the floor is untouched.

This tool does not invent transforms — it composes the three that already
exist, gated by depth:

    shell P  ← lib.admits.admit_proof_fn_bodies / admit_proof_blocks
    shell L  ← strip_specs.delete_text   (helper lemmas)
    shell D  ← strip_specs.delete_text   (spec fns)
    shell C  ← strip_specs.strip_text    (anchor header clauses)

and the worktree half reuses admit.create_admit_worktree.

THE PIN RULE (enforced here): a depth is *self-pinning* — the agent
cannot pass by weakening the guarantee — iff the top-level contract stays
frozen. P1-P3 keep it frozen. P4 strips it, so the runner REFUSES to
build a P4 state without a declared `--pin`:

    proof          the anchor's proof/lemmas stay frozen and pin the
                   reconstructed contract (express as: depth 4, empty
                   lemma/spec lists, --no-proof-admit → only the contract
                   is peeled; everything below it is the pin)
    consumer:NAME  a frozen downstream fn whose own contract forces it
    oracle:REF     a reference spec / commit to diff the reconstruction against

The pin is *recorded* in the emitted manifest; enforcing it during a run
(retaining the frozen consumer, diffing the oracle) is the runner's job,
not the builder's. The builder's contract is only: refuse P4 without one.

This is a top-level init/harness tool — a sibling of `run.py` / `admit.py`
/ `strip_specs.py`, NOT an agent skill. The proof agent never calls it.

Usage:
    # File mode — peel one file in place / to --out (debuggable like admit.py)
    python peel.py <file.rs> --depth 1 --in-place
    python peel.py <lemmas.rs> --depth 3 --in-place \\
        --lemma lemma_a --lemma lemma_b --spec-fn abstract_map
    python peel.py <file.rs> --depth 4 --in-place \\
        --contract-fn decompress --no-proof-admit --pin proof

    # Worktree mode — build a full init state from a manifest
    python peel.py --worktree /tmp/wt --gitroot /path/to/dalek-lite \\
        --ref eval/admitted-start --depth 2 --manifest decompress.peel.json
    python peel.py --worktree /tmp/wt --gitroot /path/to/dalek-lite --remove
    python peel.py --classify /path/to/wt/curve25519-dalek \\
        --classify-floor number-theory
    python peel.py --classify /path/to/wt/curve25519-dalek \\
        --classify-floor trusted-core

Manifest (worktree mode), JSON:
    {
      "name": "decompress",
      "experiment_mode": "bridge-full",
      "files": [
        {"path": "curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs",
         "lemmas": ["lemma_a", "lemma_b"],
         "spec_fns": ["abstract_map"],
         "contract_fns": ["decompress_thm"],
         "proof_op": "admit",
         "strip_proof_fns": ["helper_lemma"]}
      ],
      "pin": "proof"
    }
  Paths are relative to the worktree root. Per-file keys are all optional.
  `proof_op` is one of "admit"|"strip"|"strip-all"|"none" (default "admit");
  the legacy `proof_admit: true/false` is still accepted as an alias for
  "admit"/"none". Top-level `experiment_mode` selects the run mode.

Output (stdout): JSON summary
    file mode:     {"okay": true, "file": ..., "depth": N, "deleted": [...],
                    "stripped": [...], "proof_mode": ..., "non_axiom_admits_after": M}
    worktree mode: {"okay": true, "worktree": ..., "project": ..., "depth": N,
                    "pin": ..., "editable_files": [...], "peeled": [...]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admit  # noqa: E402  (create_admit_worktree, remove_admit_worktree, resolve_mode, admit_text)
import strip_specs  # noqa: E402  (delete_text, strip_text)
from lib.admits import count_non_axiom  # noqa: E402

# Shell names indexed by depth (depth 1 → SHELLS[0], etc.).
SHELLS = ("proofs", "lemmas", "specs", "contract")
MIN_DEPTH, MAX_DEPTH = 0, 4

# NOTE: there is deliberately NO depth→experiment-mode auto-map. An earlier
# version had one (P1→proof-only … P3→bridge-specs …); it was unsound and is
# removed. The reason is the *pin*: a cut is only an experiment mode when the
# right thing stays FROZEN to pin the reconstruction, and that is NOT a function
# of depth. `bridge-specs` deletes the shared spec (the map) but must RETAIN the
# consumer proof (e.g. montgomery::to_edwards) frozen-and-proven as the pin — yet
# cumulative depth-3 admits/strips every proof (shell P runs at depth>=1), so it
# erases that pin and the agent could reconstruct a wrong-but-contract-satisfying
# map. A monotone "strip shells 1..k" axis structurally cannot express "strip
# stratum k, keep 1..k-1 as the pin". So the run-side mode is chosen EXPLICITLY
# (manifest `experiment_mode` + a faithful (proof_op, lemmas, spec_fns) tuple),
# never derived from depth. The builder only emits `editable_files` (the frozen
# guard input) and enforces the pin rule below.


# Shell-P variants. The "proofs" shell can REMOVE proofs two ways, and they
# are NOT interchangeable — they produce different starting states and measure
# different tasks:
#   admit      hollow `proof fn` bodies / `proof { }` to admit() — GREEN start
#              (every obligation trivially discharged, verus passes), WHOLE-file.
#              This is the classic admit task (the MVP / proof-only mode).
#   strip      remove `proof { }` + standalone `assert(...)`, KEEP exec + contract
#              — RED start (postcondition unproven, verus fails), NAME-scoped to
#              the given fns. This is the "reconstruct the proof from nothing"
#              task the curated bridge/contract/ristretto rungs ran on the ANCHOR.
#   strip-all  same red strip, applied to every NON-axiom fn in the file (the
#              whole-file exec strip --strip-to-fields used on the API files);
#              axiom_* is excluded so floor-safety is structural, not by accident
#              of axiom content.
#   none       leave proofs intact (the P4 proof-pin: only the contract is peeled).
PROOF_OPS = ("admit", "strip", "strip-all", "none")


def peel_file_text(
    text: str,
    depth: int,
    *,
    path: str,
    lemmas: tuple[str, ...] = (),
    spec_fns: tuple[str, ...] = (),
    contract_fns: tuple[str, ...] = (),
    proof_op: str = "admit",
    strip_proof_fns: tuple[str, ...] = (),
    proof_admit: bool | None = None,
    proof_mode: str = "auto",
) -> tuple[str, dict]:
    """Apply shells 1..`depth` to `text`. Pure and deterministic — the same
    inputs always yield byte-identical output.

    Ordering mirrors strip_specs' own convention: structural deletions
    (shells L, D) run first so the proof pass walks post-deletion text; the
    contract strip (shell C) runs last. Shell P is governed by `proof_op`
    (see PROOF_OPS) — `admit` (green, whole-file) vs `strip`/`strip-all`
    (red, keep-exec) are DIFFERENT tasks, not implementations of one.

    `proof_admit` is a back-compat alias: True→"admit", False→"none". When
    given, it overrides `proof_op`. `path` resolves the auto admit mode only.
    """
    if not (MIN_DEPTH <= depth <= MAX_DEPTH):
        raise ValueError(f"depth must be in [{MIN_DEPTH}, {MAX_DEPTH}], got {depth}")
    if proof_admit is not None:
        proof_op = "admit" if proof_admit else "none"
    if proof_op not in PROOF_OPS:
        raise ValueError(f"proof_op must be one of {PROOF_OPS}, got {proof_op!r}")

    deleted: list[str] = []
    stripped: list[str] = []
    proof_mode_used: str | None = None
    proof_stripped: list[str] = []

    # Shells L (P2) + D (P3): delete the named lemmas / spec fns. Combined
    # into one delete pass — delete_text is name-keyed, so the union is the
    # same as two sequential passes but walks the text once.
    to_delete: set[str] = set()
    if depth >= 2:
        to_delete |= set(lemmas)
    if depth >= 3:
        to_delete |= set(spec_fns)
    if to_delete:
        text, deleted = strip_specs.delete_text(text, to_delete)

    # Shell P (P1): remove proofs per `proof_op`.
    if depth >= 1 and proof_op != "none":
        if proof_op == "admit":
            proof_mode_used = admit.resolve_mode(proof_mode, Path(path))
            text = admit.admit_text(text, proof_mode_used)
        elif proof_op == "strip":
            text, proof_stripped = strip_specs.strip_proof_from_fns(
                text, set(strip_proof_fns))
        elif proof_op == "strip-all":
            # Every fn in the file. Floor-safety (never strip an `axiom_*` body)
            # is enforced structurally inside strip_proof_from_fns, which name-
            # skips `axiom_*` — so it protects every caller (peel, the live
            # rungs, the bash launcher's --strip-proof-fn path) from one place,
            # and strip-all need not pre-filter axioms here.
            names = {m.group("name") for m in strip_specs._FN_START_RE.finditer(text)}
            text, proof_stripped = strip_specs.strip_proof_from_fns(text, names)

    # Shell C (P4): strip header clauses off the named anchors.
    if depth >= 4 and contract_fns:
        text, stripped = strip_specs.strip_text(text, only=set(contract_fns))

    report = {
        "depth": depth,
        "shells": list(SHELLS[:depth]),
        "deleted": deleted,
        "stripped": stripped,
        "proof_op": proof_op,
        "proof_stripped": proof_stripped,
        "proof_mode": proof_mode_used,
        "non_axiom_admits_after": count_non_axiom(text),
    }
    return text, report


def _require_pin(depth: int, pin: str | None, *, deletes_spec: bool = False) -> None:
    """Enforce the pin rule. A cut is *self-pinning* — the agent cannot pass by
    weakening the guarantee — only while every artifact the frozen contract is
    phrased in stays frozen. Two cuts break that and so REQUIRE a declared pin
    (a frozen-and-proven consumer, or an oracle to diff against):

      - depth 4: strips the top-level contract itself.
      - depth>=3 deleting a spec definition (e.g. the bridge map): the frozen
        contract is now phrased in an agent-reconstructed spec, so the contract
        alone under-determines it — a separate frozen consumer/oracle must pin
        the reconstructed definition. (Without this, a wrong-but-contract-
        satisfying map passes.)

    The builder cannot verify the pin is real (that the named consumer stays
    frozen-and-proven) — that is the run-side gate's job — but it refuses to
    build an unpinned state, the same discipline at the contract and the spec
    strata."""
    reasons = []
    if depth >= 4:
        reasons.append("strips the top-level contract")
    if deletes_spec:
        reasons.append("deletes a spec definition (e.g. the bridge map)")
    if reasons and not pin:
        raise ValueError(
            f"peel {' and '.join(reasons)} and is NOT self-pinning; the frozen "
            f"contract alone under-determines the reconstruction. Declare a pin "
            f"(consumer:NAME | oracle:REF | proof) and keep that consumer frozen.")


def _seal_peeled_history(wt: Path) -> str:
    """Re-root the peeled worktree on a parent-less orphan commit so the proven
    original no longer leaks through the worktree's *reachable* git history.

    `create_admit_worktree` checks out the proven `ref` at a DETACHED HEAD and
    leaves the peel edits as unstaged modifications — so `git show HEAD:<f>`,
    `git diff HEAD`, and `git log -p` all reveal the full pre-strip proof. We
    replace that HEAD with a root commit of the *peeled* tree, so those commands
    surface only the stripped state. Returns the new HEAD sha.

    Residual leak (documented; handled by run.py's GIT_RECOVERY gate, not here):
    the proven objects still live in the shared object store and the worktree's
    HEAD reflog still records the original detach, so a determined
    `git reflog` → `git show <sha>:` can still reach them. That path trips the
    recovery gate, which (once softened) rolls the round back and resets the
    session rather than handing over the answer. Seal + gate are belt and
    suspenders: the seal removes the casual leak, the gate covers the dig.
    """
    # Per-worktree branch name (was a fixed "_peeled_init"): two concurrent peel
    # builds from the same source repo share the repo's refs, so a fixed name
    # collides on `checkout --orphan`. The dest basename is unique per run-id.
    _safe = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in wt.name)
    branch = "_peeled_" + (_safe or "init")
    # A prior sealed worktree (since removed) can leave this branch ref behind
    # in the shared repo; force-delete the stale one so --orphan doesn't collide.
    try:
        admit._git("branch", "-D", branch, cwd=wt)
    except RuntimeError:
        pass
    admit._git("checkout", "--orphan", branch, cwd=wt)
    admit._git("add", "-A", cwd=wt)
    admit._git("-c", "user.email=peel@local", "-c", "user.name=peel",
               "commit", "-q", "--no-gpg-sign", "-m",
               "peeled init state (history sealed)", cwd=wt)
    sha = admit._git("rev-parse", "HEAD", cwd=wt)
    # Detach HEAD onto the commit and drop the branch ref, so this worktree
    # leaves nothing in the shared repo to collide with the next seal.
    admit._git("checkout", "--detach", sha, cwd=wt)
    admit._git("branch", "-D", branch, cwd=wt)
    return sha


def peel_worktree(
    gitroot: Path,
    ref: str,
    dest: Path,
    depth: int,
    manifest: dict,
    pin: str | None = None,
    seal: bool = True,
) -> dict:
    """Build a full peel init-state in an isolated git worktree.

    Checks out `ref` into a fresh worktree (reusing admit.create_admit_worktree
    for the git half — no admit pass there, peeling is applied per-file
    below), then applies `peel_file_text` to each manifest file in place.
    Returns a JSON-able summary including the `editable_files` set the run's
    frozen-file guard consumes (frozen = everything NOT in this set).

    `seal` (default True): re-root the worktree on an orphan commit of the
    peeled tree so the proven original does not leak through reachable history
    (see `_seal_peeled_history`). Pass False only for transform debugging.
    """
    pin = pin or manifest.get("pin")
    deletes_spec = depth >= 3 and any(
        f.get("spec_fns") for f in manifest.get("files", []))
    _require_pin(depth, pin, deletes_spec=deletes_spec)

    # Git checkout only (admit_targets=None) — peeling is applied below so a
    # single deterministic path produces every depth.
    summary = admit.create_admit_worktree(gitroot, ref, dest, admit_targets=None)
    wt = Path(summary["worktree"])

    peeled: list[dict] = []
    editable: list[str] = []
    for fspec in manifest.get("files", []):
        rel = fspec["path"]
        f = wt / rel
        if not f.exists():
            raise FileNotFoundError(f"manifest path not in worktree: {f}")
        text = f.read_text()
        new, report = peel_file_text(
            text, depth, path=str(f),
            lemmas=tuple(fspec.get("lemmas", [])),
            spec_fns=tuple(fspec.get("spec_fns", [])),
            contract_fns=tuple(fspec.get("contract_fns", [])),
            proof_op=fspec.get("proof_op", "admit"),
            strip_proof_fns=tuple(fspec.get("strip_proof_fns", [])),
            proof_admit=fspec.get("proof_admit"),  # back-compat; None if absent
        )
        f.write_text(new)
        peeled.append({"file": rel, **report})
        editable.append(rel)

    sealed_head = _seal_peeled_history(wt) if seal else None

    # Run-side handoff: only the data run.py genuinely needs — the editable set
    # (its frozen guard = everything NOT here) and the declared pin. The
    # experiment MODE is taken verbatim from the manifest if the author declared
    # one (no depth→mode inference — see the note above); the builder does not
    # invent it, because the faithful mode depends on (proof_op, pin), not depth.
    return {
        "okay": True,
        "worktree": str(wt),
        "project": summary["project"],
        "ref": ref,
        "depth": depth,
        "experiment_mode": manifest.get("experiment_mode"),
        "pin": pin,
        "name": manifest.get("name"),
        "editable_files": editable,
        "peeled": peeled,
        "sealed_head": sealed_head,
    }


# ── classify_cone: a directory-cut manifest GENERATOR ────────────────────────
# Port of launch_specgen.sh's --strip-to-fields layer boundary. This is the
# "reference oracle" classify_cone: it derives a peel manifest by DIRECTORY,
# not by dependency closure. It is a generator (emits a manifest you can edit
# and pass to --manifest), NOT a runtime dependency of the peel pipeline — the
# manifest IS the declared cone. A dependency-closure classify_cone can replace
# this later; both must emit the same manifest shape.
#
#   delete-dirs : correctness-lemma dirs above the selected floor → their
#                 non-axiom proof fns become `lemmas` (deleted at depth>=2)
#   api files   : user-facing exec files → editable, proof_admit (inline
#                 proof{} blocks hollowed at depth>=1); contract + exec kept
#   frozen      : specs (or, at the trusted-core floor, spec fn definitions), every
#                 axiom_*, and every layer at/below the selected floor — never
#                 listed, so the run's frozen guard (= everything not editable)
#                 keeps them. The spec-definition gate freezes existing spec fn
#                 bodies when a spec file becomes editable.
_CONE_ABOVE_FIELD_DELETE_DIRS = (
    "lemmas/edwards_lemmas", "lemmas/ristretto_lemmas",
    "lemmas/scalar_lemmas_", "lemmas/scalar_byte_lemmas",
)
_CONE_FIELD_DELETE_DIRS = ("lemmas/field_lemmas",)
_CONE_API_FILES = ("edwards.rs", "montgomery.rs", "ristretto.rs", "scalar.rs")
_CONE_SKIP_FILES = ("axioms.rs", "mod.rs")  # pure-axiom / module glue: stay frozen.
# Used only by the dir-cut path (_delete_dir_entries) for the field /
# number-theory floors, whose delete-dirs are lemma dirs that contain only glue
# mod.rs + pure axioms.rs — both also fail that path's `if not lemmas` gate, so
# the name skip is just an early-out there. The trusted-core whole-project walk
# (_whole_project_proof_entries) deliberately does NOT use it: it content-gates
# instead, so a proof-bearing mod.rs (curve_models/mod.rs) is peeled.
_CLASSIFY_FLOORS = ("field", "number-theory", "trusted-core")

_PROOF_FN_NAME_RE = __import__("re").compile(r"\bproof\s+fn\s+(\w+)")


def _nonaxiom_proof_fns(text: str) -> list[str]:
    """Names of `proof fn`s in `text`, excluding `axiom_*` (trusted floor)."""
    return [n for n in _PROOF_FN_NAME_RE.findall(text)
            if not n.startswith("axiom_")]


def _delete_dir_entries(project: Path, dirs: tuple[str, ...]) -> list[dict]:
    src = project / "src"
    files: list[dict] = []
    for d in dirs:
        ddir = src / d
        if not ddir.is_dir():
            continue
        for f in sorted(ddir.rglob("*.rs")):
            if f.name in _CONE_SKIP_FILES:
                continue
            lemmas = _nonaxiom_proof_fns(f.read_text())
            if not lemmas:
                continue
            files.append({
                "path": str(f.relative_to(project.parent)),
                "proof_op": "none",
                "lemmas": lemmas,
            })
    return files


def _api_strip_entries(project: Path) -> list[dict]:
    src = project / "src"
    files: list[dict] = []
    for name in _CONE_API_FILES:
        f = src / name
        if f.is_file():
            # Faithful to launch_specgen's stf_strip_all_proofs: the API exec
            # files are RED-stripped (remove inline proofs, keep contract +
            # exec), NOT admitted. Whole-file (strip-all).
            files.append({
                "path": str(f.relative_to(project.parent)),
                "proof_op": "strip-all",
            })
    return files


def _backend_field_strip_entries(project: Path) -> list[dict]:
    """Backend field exec files belong to L4: when the selected floor is L5,
    keep their exec/contracts but strip inline proof scaffolding."""
    src = project / "src"
    backend = src / "backend"
    if not backend.is_dir():
        return []
    return [
        {"path": str(f.relative_to(project.parent)), "proof_op": "strip-all"}
        for f in sorted(backend.rglob("field.rs"))
    ]


def _strip_touched_after_deletes(text: str, delete_names: list[str]) -> list[str]:
    """Names whose inline proof/assert content remains after deleting proof fns."""
    if delete_names:
        text, _ = strip_specs.delete_text(text, set(delete_names))
    names = {m.group("name") for m in strip_specs._FN_START_RE.finditer(text)}
    _, touched = strip_specs.strip_proof_from_fns(text, names)
    return touched


def _whole_project_proof_entries(project: Path) -> list[dict]:
    """Maximal in-repo proof peel: delete every non-axiom proof fn and strip
    remaining inline proof blocks/asserts from every source file.

    This is for the trusted-core floor. It keeps spec definitions, contracts,
    exec code, axiom_* bodies, assumes, and external vstd frozen; everything
    else in the repo that is proof-only becomes an editable reconstruction
    target."""
    src = project / "src"
    files: list[dict] = []
    for f in sorted(src.rglob("*.rs")):
        # CONTENT gate, not a name gate. A file is skipped only when it carries
        # no *removable* proof content — the two signals below are exactly that,
        # and both are axiom-aware: `lemmas` excludes `axiom_*`, and
        # `proof_touched` comes from strip_proof_from_fns, which name-skips
        # `axiom_*` bodies. So an `axiom_*` body that grows internal scaffolding
        # stays frozen, while a `mod.rs` with real inline proof (e.g.
        # backend/serial/curve_models/mod.rs) is peeled. The earlier by-name
        # `_CONE_SKIP_FILES` skip here false-froze that file — true glue / a
        # pure-axiom file fails the content gate below on its own, so the name
        # skip was both redundant for glue and wrong for proof-bearing mod.rs.
        text = f.read_text()
        lemmas = _nonaxiom_proof_fns(text)
        proof_touched = _strip_touched_after_deletes(text, lemmas)
        if not lemmas and not proof_touched:
            continue
        entry: dict = {"path": str(f.relative_to(project.parent))}
        if proof_touched:
            entry["proof_op"] = "strip-all"
        else:
            entry["proof_op"] = "none"
        if lemmas:
            entry["lemmas"] = lemmas
        files.append(entry)
    return files


def _merge_manifest_entries(*entry_lists: list[dict]) -> list[dict]:
    """Merge classifier entries by path, preserving first-seen order.

    Harder floors must be structural supersets of easier floors. Some entries
    are included because they currently contain proof content, while API/backend
    floor anchors are included by layer membership. Merging keeps that ladder
    cumulative without duplicating paths."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for entries in entry_lists:
        for entry in entries:
            path = entry["path"]
            if path not in merged:
                merged[path] = {"path": path}
                order.append(path)
            cur = merged[path]
            if "lemmas" in entry:
                existing = cur.setdefault("lemmas", [])
                for name in entry["lemmas"]:
                    if name not in existing:
                        existing.append(name)
            if entry.get("proof_op") == "strip-all":
                cur["proof_op"] = "strip-all"
            elif "proof_op" in entry and "proof_op" not in cur:
                cur["proof_op"] = entry["proof_op"]
    return [merged[path] for path in order]


def classify_cone(project: Path, floor: str = "field") -> dict:
    """Build a peel manifest for `project` (the curve25519-dalek member dir) by
    the directory-cut heuristic. Deterministic: files sorted, names in source
    order. Lemma files contribute their non-axiom proof fns as `lemmas`; API
    exec files are listed for proof-block stripping only.

    `floor="field"` is the historical --strip-to-fields cut: L4 field +
    L5 number theory stay frozen. `floor="number-theory"` strips through L4:
    field lemmas are deleted, backend field exec proofs are stripped, and only
    L5/common/vstd plus frozen spec vocabulary/axioms remain as the core.
    `floor="trusted-core"` strips every in-repo non-axiom proof artifact it can
    find: common number-theory lemmas, spec-module proof lemmas, backend/top-level
    proof blocks, and all higher lemmas. External vstd is never peeled."""
    if floor not in _CLASSIFY_FLOORS:
        raise ValueError(
            f"classify floor must be one of {_CLASSIFY_FLOORS}, got {floor!r}")

    if floor == "trusted-core":
        return {
            "name": "trusted-core-cut",
            "floor": floor,
            "project": str(project),
            "files": _merge_manifest_entries(
                _whole_project_proof_entries(project),
                _api_strip_entries(project),
                _backend_field_strip_entries(project),
            ),
        }

    delete_dirs = _CONE_ABOVE_FIELD_DELETE_DIRS
    if floor == "number-theory":
        delete_dirs += _CONE_FIELD_DELETE_DIRS

    files = _delete_dir_entries(project, delete_dirs)
    files.extend(_api_strip_entries(project))
    if floor == "number-theory":
        files.extend(_backend_field_strip_entries(project))

    name = "field-floor-cut" if floor == "field" else "number-theory-floor-cut"
    return {
        "name": name,
        "floor": floor,
        "project": str(project),
        "files": files,
    }


def peel_surface(manifest: dict, depth: int) -> dict:
    """Surface-as-data: describe what `depth` would do to each manifest file,
    WITHOUT touching any file. The `editable_files` list is the run's frozen
    guard input (frozen = everything not here)."""
    if not (MIN_DEPTH <= depth <= MAX_DEPTH):
        raise ValueError(f"depth must be in [{MIN_DEPTH}, {MAX_DEPTH}], got {depth}")
    per_file = []
    for f in manifest.get("files", []):
        proof_op = f.get("proof_op", "admit")
        if f.get("proof_admit") is False:
            proof_op = "none"
        per_file.append({
            "path": f["path"],
            "proof_op": proof_op if depth >= 1 else "none",
            "delete_lemmas": list(f.get("lemmas", [])) if depth >= 2 else [],
            "delete_spec_fns": list(f.get("spec_fns", [])) if depth >= 3 else [],
            "strip_contract": list(f.get("contract_fns", [])) if depth >= 4 else [],
        })
    return {
        "name": manifest.get("name"),
        "depth": depth,
        "shells": list(SHELLS[:depth]),
        "pin": manifest.get("pin"),
        "editable_files": [f["path"] for f in manifest.get("files", [])],
        "files": per_file,
    }


def _build_file_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Deterministic peel-depth init-state builder "
                    "(file mode), or a peel-state git worktree (--worktree).")
    ap.add_argument("target", type=Path, nargs="?",
                    help="Target .rs file to peel (file mode)")
    ap.add_argument("--depth", type=int, default=1,
                    help="Peel depth 1..4 (default: 1 = proofs only)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--out", type=Path, help="Write output here (file mode)")
    grp.add_argument("--in-place", action="store_true",
                     help="Overwrite the target file in place (file mode)")
    # Per-file shell targets (file mode).
    ap.add_argument("--lemma", action="append", default=[], metavar="NAME",
                    help="Helper lemma to delete at depth>=2 (repeatable).")
    ap.add_argument("--spec-fn", action="append", default=[], metavar="NAME",
                    help="Spec fn to delete at depth>=3 (repeatable).")
    ap.add_argument("--contract-fn", action="append", default=[], metavar="NAME",
                    help="Anchor whose header clauses are stripped at depth>=4 "
                         "(repeatable).")
    ap.add_argument("--proof-op", choices=list(PROOF_OPS), default="admit",
                    help="Shell-P variant: admit (green, whole-file, default) | "
                         "strip (red, keep-exec, name-scoped via --strip-proof-fn) "
                         "| strip-all (red, every fn in the file) | none.")
    ap.add_argument("--strip-proof-fn", action="append", default=[], metavar="NAME",
                    help="With --proof-op strip: the fn whose inline proof to "
                         "remove (repeatable). Name-scoped, keeps exec + contract.")
    ap.add_argument("--no-proof-admit", action="store_true",
                    help="Alias for --proof-op none. Use to express a proof-pinned "
                         "P4: only the contract is peeled, the proof infrastructure "
                         "below stays frozen as the pin.")
    ap.add_argument("--pin", default=None,
                    help="Soundness pin (required at depth 4): "
                         "proof | consumer:NAME | oracle:REF.")
    # Worktree mode.
    wt = ap.add_argument_group("worktree mode (--worktree)")
    wt.add_argument("--worktree", type=Path, metavar="DEST",
                    help="Build (or --remove) a peel-state git worktree at DEST.")
    wt.add_argument("--gitroot", type=Path, metavar="REPO",
                    help="Project git repo to worktree from. Required with --worktree.")
    wt.add_argument("--ref", default="eval/admitted-start",
                    help="Commit/branch the worktree checks out "
                         "(default: eval/admitted-start).")
    wt.add_argument("--manifest", type=Path,
                    help="Peel manifest JSON (worktree mode). Required unless --remove.")
    wt.add_argument("--remove", action="store_true",
                    help="Remove the worktree at DEST instead of building it.")
    wt.add_argument("--no-seal-history", action="store_true",
                    help="Skip re-rooting the worktree on an orphan commit. "
                         "By default the peeled tree is sealed so the proven "
                         "original does not leak via git show/diff/log -p; pass "
                         "this only for transform debugging.")
    # Inspection (no mutation): generate / preview a manifest.
    ins = ap.add_argument_group("inspection (no files touched)")
    ins.add_argument("--classify", type=Path, metavar="PROJECT",
                     help="Emit a directory-cut peel manifest for the "
                          "curve25519-dalek member dir (classify_cone generator).")
    ins.add_argument("--classify-floor", choices=list(_CLASSIFY_FLOORS),
                     default="field",
                     help="Floor for --classify: field keeps L4+L5 frozen "
                          "(default); number-theory strips through L4 field "
                          "and freezes the L5/common/vstd core plus specs/axioms; "
                          "trusted-core strips every in-repo non-axiom proof "
                          "artifact and freezes spec definitions, exec "
                          "contracts/code, axioms/assumes, and external vstd.")
    ins.add_argument("--surface", action="store_true",
                     help="Print the peel surface for --manifest at --depth "
                          "(editable files + per-file ops) without touching files.")
    return ap


def main() -> int:
    ap = _build_file_parser()
    args = ap.parse_args()

    # ── Inspection mode (no mutation): classify / surface ────────────────────
    if args.classify is not None:
        project = args.classify
        if not (project / "src").is_dir():
            print(json.dumps({"okay": False,
                              "error": f"no src/ under {project} "
                                       "(pass the curve25519-dalek member dir)"}))
            return 1
        print(json.dumps(
            {"okay": True, **classify_cone(project, args.classify_floor)},
            indent=2))
        return 0
    if args.surface:
        if args.manifest is None:
            ap.error("--surface requires --manifest")
        try:
            manifest = json.loads(args.manifest.read_text())
            print(json.dumps({"okay": True, **peel_surface(manifest, args.depth)},
                             indent=2))
            return 0
        except (ValueError, OSError) as e:
            print(json.dumps({"okay": False, "error": str(e)}, indent=2))
            return 1

    # ── Worktree mode ────────────────────────────────────────────────────────
    if args.worktree is not None:
        if args.gitroot is None:
            ap.error("--worktree requires --gitroot (the project repo root)")
        try:
            if args.remove:
                admit.remove_admit_worktree(args.gitroot, args.worktree)
                print(json.dumps({"okay": True, "removed": str(args.worktree)},
                                 indent=2))
                return 0
            if args.manifest is None:
                ap.error("--worktree (build) requires --manifest")
            manifest = json.loads(args.manifest.read_text())
            summary = peel_worktree(
                args.gitroot, args.ref, args.worktree, args.depth,
                manifest, pin=args.pin, seal=not args.no_seal_history)
            print(json.dumps(summary, indent=2))
            return 0
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            print(json.dumps({"okay": False, "error": str(e)}, indent=2))
            return 1

    # ── File mode (default) ──────────────────────────────────────────────────
    if args.target is None or not (args.in_place or args.out):
        ap.error("file mode requires <target> and one of --in-place / --out "
                 "(or use --worktree for worktree mode)")
    if not args.target.exists():
        print(json.dumps({"okay": False,
                          "error": f"target not found: {args.target}"}))
        return 1
    try:
        _require_pin(args.depth, args.pin,
                     deletes_spec=args.depth >= 3 and bool(args.spec_fn))
    except ValueError as e:
        print(json.dumps({"okay": False, "error": str(e)}, indent=2))
        return 1

    text = args.target.read_text()
    proof_op = "none" if args.no_proof_admit else args.proof_op
    new, report = peel_file_text(
        text, args.depth, path=str(args.target),
        lemmas=tuple(args.lemma), spec_fns=tuple(args.spec_fn),
        contract_fns=tuple(args.contract_fn),
        proof_op=proof_op, strip_proof_fns=tuple(args.strip_proof_fn))
    dest = args.target if args.in_place else args.out
    dest.write_text(new)

    print(json.dumps({
        "okay": True,
        "file": str(args.target),
        "out": str(dest),
        "pin": args.pin,
        "changed": new != text,
        "bytes_before": len(text),
        "bytes_after": len(new),
        **report,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
