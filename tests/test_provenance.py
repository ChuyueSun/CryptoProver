"""Regression coverage for source-tree and gate receipt identity."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.provenance import (  # noqa: E402
    accepted_promotion_tree_hash,
    canonical_json_bytes,
    gate_signature,
    derive_lineage_id,
    receipt_id,
    receipt_key,
    sha256_file,
    source_tree_receipt,
    supervisor_package_id,
    reusable_seed_authority,
    validate_credential_identity,
    validate_launch_registration,
    validate_campaign_spec,
    write_immutable_json,
)
import lib.provenance as provenance  # noqa: E402


class ProvenanceReceiptTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        (root / "Cargo.toml").write_text("[workspace]\nmembers = ['crate']\n")
        project = root / "crate"
        (project / "src").mkdir(parents=True)
        (project / "Cargo.toml").write_text("[package]\nname='crate'\nversion='0.1.0'\n")
        (project / "src" / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        return project

    def test_supervisor_package_id_binds_full_content_but_not_itself(self):
        package = {
            "schema_version": 1,
            "launch_argv": ["python3", "run.py"],
            "immutable_inputs": [{"path": "/x", "sha256": "abc"}],
        }
        identity = supervisor_package_id(package)
        expected = hashlib.sha256(
            b"trusted-core-supervisor-package:v1\x00"
            + canonical_json_bytes(package)
        ).hexdigest()
        self.assertEqual(identity, expected)
        self.assertNotEqual(identity, receipt_id(package))
        self.assertEqual(
            identity,
            supervisor_package_id({"package_id": "stale", **package}),
        )
        self.assertEqual(
            identity,
            supervisor_package_id(dict(reversed(list(package.items())))),
        )
        changed = json.loads(json.dumps(package))
        changed["launch_argv"].append("--different")
        self.assertNotEqual(identity, supervisor_package_id(changed))

    def test_tree_hash_covers_workspace_source_but_not_generated_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            first = source_tree_receipt(project)
            (project / "target").mkdir()
            (project / "target" / "noise").write_text("generated")
            self.assertEqual(first["tree_hash"], source_tree_receipt(project)["tree_hash"])
            (Path(td) / ".git").write_text(
                "gitdir: /path/that/depends/on/worktree/location\n"
            )
            self.assertEqual(first["tree_hash"], source_tree_receipt(project)["tree_hash"])
            (project / "src" / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            self.assertEqual(second["file_count"], len(second["files"]))

    def test_legacy_walk_order_and_hash_are_preserved_without_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            root = provenance.workspace_root(project)
            legacy_files = [
                provenance._file_entry(path, root)
                for path in provenance._iter_relevant_files(root)
            ]
            receipt = source_tree_receipt(project)
            self.assertEqual(receipt["files"], legacy_files)
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(
                set(receipt),
                {"schema_version", "workspace_root", "file_count", "tree_hash", "files"},
            )
            self.assertEqual(
                receipt["tree_hash"],
                hashlib.sha256(canonical_json_bytes(legacy_files)).hexdigest(),
            )

    def test_literal_inputs_below_pruned_dirs_are_appended_and_recursive(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            generated = project / "target"
            generated.mkdir()
            (generated / "one.rs").write_text(
                'include!(r#"two.rs"#);\npub const ONE: u8 = 1;\n')
            (generated / "two.rs").write_text("pub const TWO: u8 = 2;\n")
            (project / "src" / "lib.rs").write_text(
                '#[path = r"../target/one.rs"] mod one;\n')
            receipt = source_tree_receipt(project)
            self.assertEqual(
                receipt["literal_compiler_inputs"],
                ["crate/target/one.rs", "crate/target/two.rs"],
            )
            before = receipt["tree_hash"]
            (generated / "two.rs").write_text("pub const TWO: u8 = 9;\n")
            self.assertNotEqual(before, source_tree_receipt(project)["tree_hash"])

    def test_literal_metadata_marks_input_already_in_ordinary_walk(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            blob = project / "src" / "blob.dat"
            blob.write_bytes(b"one")
            (project / "src" / "lib.rs").write_text(
                'pub const BLOB: &[u8] = include_bytes!("blob.dat");\n')
            receipt = source_tree_receipt(project)
            self.assertIn("crate/src/blob.dat", receipt["literal_compiler_inputs"])
            self.assertEqual(
                sum(entry["path"] == "crate/src/blob.dat"
                    for entry in receipt["files"]),
                1,
            )

    def test_manifest_targets_and_path_crates_override_generated_dir_pruning(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            (Path(td) / "Cargo.toml").write_text(
                "[workspace]\nmembers=['crate', 'results/member']\n")
            (project / "Cargo.toml").write_text(
                "[package]\nname='crate'\nversion='0.1.0'\n"
                "build='results/build.rs'\n"
                "[lib]\npath='results/lib.rs'\n"
                "[dependencies.helper]\npath='target/helper'\n"
            )
            results = project / "results"
            results.mkdir()
            (results / "build.rs").write_text("fn main() {}\n")
            (results / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
            helper = project / "target" / "helper"
            (helper / "src").mkdir(parents=True)
            (helper / "Cargo.toml").write_text(
                "[package]\nname='helper'\nversion='0.1.0'\n")
            (helper / "src" / "lib.rs").write_text("pub fn helper() {}\n")
            member = Path(td) / "results" / "member"
            (member / "src").mkdir(parents=True)
            (member / "Cargo.toml").write_text(
                "[package]\nname='member'\nversion='0.1.0'\n")
            (member / "src" / "lib.rs").write_text("pub fn member() {}\n")
            receipt = source_tree_receipt(project)
            expected = {
                "crate/results/build.rs", "crate/results/lib.rs",
                "crate/target/helper/Cargo.toml",
                "crate/target/helper/src/lib.rs",
                "results/member/Cargo.toml", "results/member/src/lib.rs",
            }
            self.assertTrue(expected <= set(receipt["cargo_compiler_inputs"]))
            before = receipt["tree_hash"]
            (results / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
            self.assertNotEqual(before, source_tree_receipt(project)["tree_hash"])

    def test_verilib_is_noise_unless_explicitly_declared(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            hidden = project / ".verilib"
            hidden.mkdir()
            generated = hidden / "generated.rs"
            generated.write_text("pub const V: u8 = 1;\n")
            stable = source_tree_receipt(project)["tree_hash"]
            generated.write_text("pub const V: u8 = 2;\n")
            self.assertEqual(stable, source_tree_receipt(project)["tree_hash"])
            (project / "src" / "lib.rs").write_text(
                'include!("../.verilib/generated.rs");\n')
            explicit = source_tree_receipt(project)
            self.assertIn(
                "crate/.verilib/generated.rs",
                {entry["path"] for entry in explicit["files"]},
            )

    def test_literal_and_manifest_workspace_escapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ext:
            project = self._workspace(Path(td))
            external = Path(ext) / "outside.rs"
            external.write_text("pub fn outside() {}\n")
            (project / "src" / "lib.rs").write_text(
                f'include!("{external}");\n')
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                source_tree_receipt(project)
            (project / "src" / "lib.rs").write_text("pub fn okay() {}\n")
            (project / "Cargo.toml").write_text(
                "[package]\nname='crate'\nversion='0.1.0'\n"
                f"[lib]\npath='{external}'\n")
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                source_tree_receipt(project)

    def test_nonexistent_comment_literal_does_not_false_reject(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            (project / "src" / "lib.rs").write_text(
                '/// Example: include_str!("../../../etc/not-a-real-input")\n'
                "pub fn okay() {}\n")
            receipt = source_tree_receipt(project)
            self.assertNotIn("literal_compiler_inputs", receipt)

    def test_absolute_workspace_member_inside_root_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = self._workspace(root)
            member = root / "results" / "absolute-member"
            (member / "src").mkdir(parents=True)
            (member / "Cargo.toml").write_text(
                "[package]\nname='absolute-member'\nversion='0.1.0'\n")
            (member / "src" / "lib.rs").write_text("pub fn member() {}\n")
            (root / "Cargo.toml").write_text(
                f"[workspace]\nmembers=['crate', '{member}']\n")
            receipt = source_tree_receipt(project)
            self.assertIn(
                "results/absolute-member/src/lib.rs",
                set(receipt["cargo_compiler_inputs"]),
            )

    def test_manifest_declared_source_outside_src_is_covered(self):
        # Pruning artifact NAMES at any depth outside a `src` segment is a
        # naming heuristic. Cargo lets a manifest point [lib] path anywhere,
        # so `custom/results/lib.rs` is real compiler input. Prune by POSITION
        # (a directory holding a Cargo.toml) instead of by name-plus-`src`.
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            (project / "Cargo.toml").write_text(
                "[package]\nname='crate'\nversion='0.1.0'\n"
                "\n[lib]\npath='custom/results/lib.rs'\n"
            )
            declared = project / "custom" / "results"
            declared.mkdir(parents=True)
            (declared / "lib.rs").write_text("pub fn v() -> u8 { 1 }\n")
            first = source_tree_receipt(project)
            self.assertIn(
                "crate/custom/results/lib.rs",
                {e["path"] for e in first["files"]},
            )
            self.assertIn(
                "crate/custom/results/lib.rs",
                first["cargo_compiler_inputs"],
            )
            (declared / "lib.rs").write_text("pub fn v() -> u8 { 2 }\n")
            self.assertNotEqual(
                first["tree_hash"], source_tree_receipt(project)["tree_hash"])

            # Artifact dirs beside a manifest are still excluded.
            stable = source_tree_receipt(project)["tree_hash"]
            (project / "target").mkdir()
            (project / "target" / "junk").write_text("generated")
            (Path(td) / "results").mkdir()
            (Path(td) / "results" / "junk").write_text("generated")
            self.assertEqual(
                stable, source_tree_receipt(project)["tree_hash"])

    def test_nested_link_defects_reject_even_under_a_pruned_target(self):
        # Dispatching on exists()/is_dir() before strict resolution absorbed a
        # dangling or self-cyclic NESTED link into "not-a-source-target", so
        # the rejection fired only when the target dir happened to be walked
        # independently — never under a pruned one. Also covers a true cycle,
        # which the earlier rejection test did not exercise.
        for name, make in (
            ("dangling", lambda p: (p / "bad").symlink_to(p / "missing")),
            ("selfcycle", lambda p: (p / "bad").symlink_to(p / "bad")),
        ):
            with self.subTest(defect=name):
                with tempfile.TemporaryDirectory() as td:
                    project = self._workspace(Path(td))
                    pruned = Path(td) / "results" / "inside"
                    pruned.mkdir(parents=True)
                    make(pruned)
                    (project / "src" / "oracle").symlink_to(pruned.resolve())
                    with self.assertRaisesRegex(
                        ValueError, "dangling or cyclic",
                    ):
                        source_tree_receipt(project)

        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            loop = Path(td) / "loopdir"
            loop.mkdir()
            (loop / "self").symlink_to(loop.resolve())
            (project / "src" / "oracle").symlink_to(loop.resolve())
            with self.assertRaisesRegex(ValueError, "cycle"):
                source_tree_receipt(project)

    def test_nested_artifact_named_dirs_are_covered_source(self):
        # T315 H3a: `results`/`target`/`claude_*` are run artifacts only at
        # the workspace ROOT. A crate module named src/results/ is verifiable
        # source and must change the tree hash.
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            first = source_tree_receipt(project)
            nested = project / "src" / "results"
            nested.mkdir()
            (nested / "mod.rs").write_text("pub fn hidden() -> u8 { 3 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            paths = {entry["path"] for entry in second["files"]}
            self.assertIn("crate/src/results/mod.rs", paths)
            # Root-level artifact dirs remain excluded.
            (Path(td) / "results").mkdir()
            (Path(td) / "results" / "noise.json").write_text("{}")
            self.assertEqual(
                second["tree_hash"], source_tree_receipt(project)["tree_hash"],
            )

    def test_symlinked_directory_is_visible_in_receipt(self):
        # T315 H3b: os.walk(followlinks=False) never yields a symlinked dir
        # as a file, so it was absent from the receipt entirely — an
        # oracle-smuggling channel. It must appear as a symlink entry and
        # adding/retargeting it must change the tree hash.
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "oracle.rs").write_text("pub fn proven() {}\n")
            first = source_tree_receipt(project)
            (project / "src" / "extra").symlink_to(outside)
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            entries = {e["path"]: e for e in second["files"]}
            self.assertIn("crate/src/extra", entries)
            self.assertEqual(entries["crate/src/extra"]["kind"], "symlink")

    def test_symlink_binds_target_bytes_not_just_link_text(self):
        # Hashing only the link TEXT binds the pointer, not the bytes: a link
        # into an out-of-tree directory kept a constant tree hash while its
        # target — real compiler input — was edited freely. Both the retarget
        # and the target mutation must move the hash.
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "oracle.rs").write_text("pub fn o() -> u8 { 1 }\n")
            (project / "src" / "oracle").symlink_to(outside)
            first = source_tree_receipt(project)
            entry = [e for e in first["files"] if e["kind"] == "symlink"]
            self.assertEqual(len(entry), 1)
            self.assertIn("target_sha256", entry[0])

            # Mutating the out-of-tree target changes the receipt.
            (outside / "oracle.rs").write_text("pub fn o() -> u8 { 99 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])

            # Adding a file under the target directory also changes it.
            (outside / "more.rs").write_text("pub fn m() {}\n")
            third = source_tree_receipt(project)
            self.assertNotEqual(second["tree_hash"], third["tree_hash"])

            # A file symlink binds its target's bytes too.
            (outside / "leaf.rs").write_text("pub fn leaf() -> u8 { 1 }\n")
            (project / "src" / "leaf.rs").symlink_to(outside / "leaf.rs")
            fourth = source_tree_receipt(project)
            (outside / "leaf.rs").write_text("pub fn leaf() -> u8 { 2 }\n")
            self.assertNotEqual(
                fourth["tree_hash"], source_tree_receipt(project)["tree_hash"],
            )

    def test_symlink_closure_binds_logical_paths_not_a_basename_multiset(self):
        # Hashing a sorted multiset of BASENAMES lets two same-named files
        # under different logical directories swap contents invisibly — the
        # receipt is identical before and after. The closure must bind the
        # logical traversal path.
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            inside = Path(td) / "inside"
            (inside / "a").mkdir(parents=True)
            (inside / "b").mkdir(parents=True)
            (inside / "a" / "foo.rs").write_text("pub fn x() -> u8 { 1 }\n")
            (inside / "b" / "foo.rs").write_text("pub fn y() -> u8 { 2 }\n")
            (project / "src" / "oracle").symlink_to(inside)
            before = source_tree_receipt(project)["tree_hash"]
            a_text = (inside / "a" / "foo.rs").read_text()
            (inside / "a" / "foo.rs").write_text(
                (inside / "b" / "foo.rs").read_text())
            (inside / "b" / "foo.rs").write_text(a_text)
            self.assertNotEqual(
                before, source_tree_receipt(project)["tree_hash"])

    def test_escaping_dangling_and_cyclic_symlinks_are_rejected(self):
        # Out-of-workspace targets cannot be bound to the tree identity, and
        # hashing them means walking an unbounded external filesystem (a link
        # to "/" would hash the machine). Reject rather than hash — the same
        # rule the literal-include escape already uses.
        with tempfile.TemporaryDirectory() as td, \
                tempfile.TemporaryDirectory() as external:
            project = self._workspace(Path(td))
            (Path(external) / "oracle.rs").write_text("pub fn o() {}\n")
            link = project / "src" / "oracle"
            link.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                source_tree_receipt(project)
            link.unlink()

            # A link to the filesystem root is rejected without traversal.
            root_link = project / "src" / "everything"
            root_link.symlink_to(Path("/"))
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                source_tree_receipt(project)
            root_link.unlink()

            # Dangling.
            dead = project / "src" / "dead"
            dead.symlink_to(Path(td) / "nope")
            with self.assertRaisesRegex(ValueError, "dangling or cyclic"):
                source_tree_receipt(project)
            dead.unlink()

            # A nested escaping link inside an otherwise-confined target.
            inside = Path(td) / "inside"
            inside.mkdir()
            (inside / "sneaky.rs").symlink_to(
                Path(external) / "oracle.rs")
            (project / "src" / "oracle").symlink_to(inside)
            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                source_tree_receipt(project)

    def test_linked_worktree_control_file_cannot_change_source_hash(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            project = self._workspace(repo)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.email=test@example.com",
                    "-c", "user.name=test",
                    "commit", "-q", "-m", "source",
                ],
                check=True,
            )
            first = base / "a"
            second = base / "worktree-with-a-different-path-length"
            for destination in (first, second):
                subprocess.run(
                    [
                        "git", "-C", str(repo), "worktree", "add", "-q",
                        "--detach", str(destination), "HEAD",
                    ],
                    check=True,
                )
            self.assertNotEqual(
                (first / ".git").read_text(),
                (second / ".git").read_text(),
            )
            self.assertEqual(
                source_tree_receipt(first / project.name)["tree_hash"],
                source_tree_receipt(second / project.name)["tree_hash"],
            )

    def test_gate_signature_and_immutable_write_bind_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tool = root / "verus_check.py"
            tool.write_text("print('one')\n")
            one = gate_signature(["python3", str(tool), "--rlimit", "80"], tool_paths=[tool])
            two = gate_signature(["python3", str(tool), "--rlimit", "90"], tool_paths=[tool])
            self.assertNotEqual(one["signature"], two["signature"])
            self.assertNotEqual(receipt_key("tree-a", one["signature"]), receipt_key("tree-b", one["signature"]))
            out = root / "receipt.json"
            value = {"tree_hash": "tree-a", "gate": one}
            write_immutable_json(out, value)
            write_immutable_json(out, value)
            with self.assertRaises(RuntimeError):
                write_immutable_json(out, {"tree_hash": "tree-b"})
            self.assertEqual(json.loads(out.read_text())["tree_hash"], "tree-a")

    def test_only_accepted_reusable_promotion_can_authorize_a_seed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted = root / "accepted.json"
            accepted.write_text(json.dumps({
                "decision": "ACCEPTED",
                "terminal_disposition": {"state": "ACCEPTED", "reusable": True},
                "final_tree_receipt": {"tree_hash": "tree-good"},
            }))
            self.assertEqual(accepted_promotion_tree_hash(accepted), "tree-good")

            rejected = root / "rejected.json"
            rejected.write_text(json.dumps({
                "decision": "REJECTED",
                "terminal_disposition": {"state": "REJECTED_DRIFTED", "reusable": False},
                "final_tree_receipt": {"tree_hash": "tree-rejected"},
            }))
            with self.assertRaises(ValueError):
                accepted_promotion_tree_hash(rejected)

    def test_receipt_id_is_path_independent_and_content_sensitive(self):
        base = {"schema_version": 2, "tree_hash": "tree-a"}
        first = {**base, "receipt_path": "/one"}
        second = {**base, "receipt_path": "/two"}
        self.assertEqual(receipt_id(first), receipt_id(second))
        self.assertNotEqual(
            receipt_id(first),
            receipt_id({**second, "tree_hash": "tree-b"}),
        )
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            b'{"a":1,"b":2}',
        )

    def test_campaign_spec_binds_manifest_hash_and_census(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "peel_manifests"
            manifest_dir.mkdir()
            manifest = manifest_dir / "cut.json"
            manifest.write_text(json.dumps({
                "files": [
                    {"path": "a.rs", "lemmas": ["one", "two"]},
                    {"path": "b.rs", "proof_op": "strip-all"},
                ],
            }))
            spec = manifest_dir / "campaign.json"
            spec.write_text(json.dumps({
                "schema_version": 1,
                "campaign_id": "test",
                "status": "pre_registered",
                "task": {
                    "manifest_path": "peel_manifests/cut.json",
                    "manifest_sha256": sha256_file(manifest),
                    "manifest_file_count": 2,
                    "deleted_named_lemma_count": 2,
                    "source_ref": "ref",
                    "source_commit": "commit",
                    "source_tree": "tree",
                    "expected_pre_tree_hash": "a" * 64,
                    "expected_post_peel_tree_hash": "b" * 64,
                },
                "stopping_policy": {
                    "k": None,
                    "max_cost_usd": None,
                    "max_wall_seconds": None,
                },
            }))
            validated = validate_campaign_spec(spec, repo_root=root)
            self.assertEqual(validated["manifest_file_count"], 2)
            self.assertEqual(
                validated["unresolved_budget_fields"],
                ["k", "max_cost_usd", "max_wall_seconds"],
            )
            with self.assertRaisesRegex(ValueError, "budget is not authorized"):
                validate_campaign_spec(
                    spec, repo_root=root, require_launch_budget=True,
                )

            manifest.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
                validate_campaign_spec(spec, repo_root=root)

    def test_launch_registration_binds_runtime_and_budget(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "launch.json"
            campaign = {
                "campaign_id": "campaign",
                "campaign_spec_sha256": "campaign-sha",
                "manifest_sha256": "manifest-sha",
            }
            value = {
                "schema_version": 1,
                "scoreable": True,
                "campaign_id": "campaign",
                "campaign_spec_sha256": "campaign-sha",
                "manifest_sha256": "manifest-sha",
                "container_image_digest": "image",
                "harness_git_commit": "commit",
                "harness_source_tree_sha256": "tree",
                "provider_proxy_policy_sha256": "policy",
                "credential_identity": {
                    "kind": "claude_code_oauth",
                    "algorithm": "sha256-salt-token-v1",
                    "salt_hex": "a" * 64,
                    "digest": hashlib.sha256(
                        bytes.fromhex("a" * 64) + b"oauth-token"
                    ).hexdigest(),
                },
                "hint_level": "H0",
                "execution": {
                    "agent_backend": "claude",
                    "model": "claude-haiku-4-5",
                    "rounds": 10,
                    "max_task_minutes": 60,
                    "verus_rlimit": 80,
                    "cargo_jobs": 4,
                    "cpu_shares": 1024,
                    "memory": "8g",
                    "max_parallel": 1,
                    "agent_max_turns": 50,
                    "experiment_mode": "field-floor",
                },
                "budget": {
                    "authorization_id": "user-1",
                    "max_cost_usd": 1000,
                    "max_wall_seconds": 604800,
                    "plateau_k": 4,
                },
            }
            path.write_text(json.dumps(value))
            result = validate_launch_registration(
                path, campaign=campaign,
                actual_image_digest="image",
                actual_harness_commit="commit",
                actual_harness_tree_sha256="tree",
                actual_proxy_policy_sha256="policy",
            )
            self.assertEqual(
                result["credential_identity"]["kind"], "claude_code_oauth")
            validate_credential_identity(value, "oauth-token")
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate_credential_identity(value, "other-token")
            missing_identity = {**value}
            missing_identity.pop("credential_identity")
            path.write_text(json.dumps(missing_identity))
            with self.assertRaisesRegex(ValueError, "credential_identity"):
                validate_launch_registration(path, campaign=campaign)
            self.assertEqual(result["registration_id"], receipt_id(value))
            missing_turn_cap = json.loads(json.dumps(value))
            missing_turn_cap["execution"].pop("agent_max_turns")
            path.write_text(json.dumps(missing_turn_cap))
            with self.assertRaisesRegex(ValueError, "agent_max_turns"):
                validate_launch_registration(path, campaign=campaign)
            value["budget"]["max_cost_usd"] = None
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "must be positive"):
                validate_launch_registration(path, campaign=campaign)

    def test_banked_partial_requires_fresh_exact_gate_and_vector(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bank.json"
            value = {
                "schema_version": 2,
                "decision": "BANKED_PARTIAL",
                "scoreable": True,
                "lineage_id": "lineage",
                "campaign_spec_sha256": "campaign",
                "terminal_disposition": {
                    "state": "BANKED_PARTIAL",
                    "reusable": True,
                },
                "final_tree_receipt": {"tree_hash": "tree"},
                "banking_gate_receipt": {
                    "fresh": True,
                    "exact_tree_match": True,
                    "vector": {
                        "hard_admits": 4,
                        "verification_errors": 3,
                        "resource_limits": 2,
                        "raw_errors": 5,
                    },
                },
            }
            value["receipt_id"] = receipt_id(value)
            path.write_text(json.dumps(value))
            authority = reusable_seed_authority(path)
            self.assertEqual(authority["kind"], "BANKED_PARTIAL")
            self.assertEqual(authority["tree_hash"], "tree")
            self.assertEqual(
                derive_lineage_id("campaign", "start"),
                derive_lineage_id("campaign", "start"),
            )

            value["banking_gate_receipt"]["fresh"] = False
            value["receipt_id"] = receipt_id(value)
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "fresh exact-tree"):
                reusable_seed_authority(path)


if __name__ == "__main__":
    unittest.main()
