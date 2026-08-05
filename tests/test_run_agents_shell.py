import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_AGENTS = ROOT / "docker" / "run_agents.sh"


def shell_function(source: str, name: str, following_marker: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(following_marker, start)
    return source[start:end].rstrip()


class RunAgentsShellTests(unittest.TestCase):
    def test_trusted_core_rate_limit_is_not_shadowed_by_terminal_validation(self):
        source = RUN_AGENTS.read_text()
        kill_agent_tap = shell_function(
            source, "_kill_agent_tap", "\n# File-based, NOT array-based:"
        )
        reap = shell_function(
            source, "reap", "\n# ---- offline dependency/toolchain preflight gate"
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                sweep_dir={tmp_path / "sweep"}
                results={tmp_path / "results"}
                RUN_ID=run
                TRUSTED_CORE_PROFILE=1
                PROFILE_MAX_COST_USD=100
                PROFILE_MAX_WALL_SECONDS=1000
                PROFILE_PLATEAU_K=3
                CAMPAIGN_STATE=
                repo={tmp_path / "repo"}
                RATE_LIMITED=0
                PROFILE_FAILED=0
                mkdir -p "$sweep_dir/agent_0" "$results/$RUN_ID/target"
                printf '{{"end_reason":"RATE_LIMITED"}}\n' > "$results/$RUN_ID/target/result.json"

                _kill_tap_proc() {{ return 0; }}
                {kill_agent_tap}

                docker() {{
                    case "$1" in
                        wait) printf '42\n' ;;
                        logs|rm) return 0 ;;
                        *) return 1 ;;
                    esac
                }}
                ledger_set() {{ printf '%s=%s\n' "$1" "$2" >> "$sweep_dir/ledger"; }}
                _finalize_profile_audit() {{
                    mkdir -p "$results/$RUN_ID"
                    printf '{{"cost_status":"complete","counts":{{"unresolved_streams":0}}}}\n' > "$results/$RUN_ID/usage_audit.json"
                }}
                python3() {{
                    if [ "$1" = "-c" ]; then
                        printf 'RATE_LIMITED\n'
                        return 0
                    fi
                    if [ "$1" = "-" ]; then
                        cat >/dev/null
                        return 0
                    fi
                    printf 'unexpected terminal validation\n' >&2
                    return 99
                }}
                {reap}

                reap agent-run-0 0 target "$results" /work member
                grep -q '^target=RATE_LIMITED$' "$sweep_dir/ledger"
                test "$RATE_LIMITED" = 1
                test "$PROFILE_FAILED" = 0
                test ! -e "$sweep_dir/agent_0/terminal_validation.json"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                text=True,
                capture_output=True,
                env={**os.environ, "LC_ALL": "C"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "MARKER idx=0 target=target rc=42 end_reason=RATE_LIMITED",
            result.stdout,
        )

    def test_retryable_profile_outcomes_bypass_reusable_terminal_validation(self):
        source = RUN_AGENTS.read_text()
        start = source.index(
            'elif [ "$end_reason" = "PREMODEL_GATE_INDETERMINATE" ]'
        )
        end = source.index('elif [ ! -f "$result_json" ]', start)
        retry_branch = source[start:end]
        self.assertIn("RATE_LIMIT_OR_HANG", retry_branch)
        self.assertIn("TRANSPORT_ERROR", retry_branch)
        self.assertIn("RETRY_EXHAUSTED", retry_branch)
        self.assertIn('audit.get("cost_status") == "complete"', retry_branch)
        self.assertNotIn("validate-terminal", retry_branch)

    def test_no_tap_trusted_core_limit_reap_reaches_receipts_and_marker(self):
        source = RUN_AGENTS.read_text()
        kill_agent_tap = shell_function(
            source, "_kill_agent_tap", "\n# File-based, NOT array-based:"
        )
        reap = shell_function(
            source, "reap", "\n# ---- offline dependency/toolchain preflight gate"
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                sweep_dir={tmp_path / "sweep"}
                results={tmp_path / "results"}
                RUN_ID=run
                TRUSTED_CORE_PROFILE=1
                PROFILE_MAX_COST_USD=100
                PROFILE_MAX_WALL_SECONDS=1000
                PROFILE_PLATEAU_K=3
                CAMPAIGN_STATE=
                repo={tmp_path / "repo"}
                mkdir -p "$sweep_dir/agent_0" "$results/$RUN_ID/target"
                printf '{{"end_reason":"LIMIT"}}\\n' > "$results/$RUN_ID/target/result.json"

                _kill_tap_proc() {{ return 0; }}
                {kill_agent_tap}

                docker() {{
                    case "$1" in
                        wait) printf '1\\n' ;;
                        logs|rm) return 0 ;;
                        *) return 1 ;;
                    esac
                }}
                ledger_set() {{ printf '%s=%s\\n' "$1" "$2" >> "$sweep_dir/ledger"; }}
                _finalize_profile_audit() {{
                    mkdir -p "$results/$RUN_ID"
                    printf '{{"cost_status":"complete"}}\\n' > "$results/$RUN_ID/usage_audit.json"
                }}
                python3() {{
                    if [ "$1" = "-c" ]; then
                        case "$2" in
                            *end_reason*) printf 'LIMIT\\n' ;;
                            *decision*) printf 'BANKED_PARTIAL\\n' ;;
                            *) return 1 ;;
                        esac
                        return 0
                    fi
                    case "$2" in
                        validate-terminal|advance-state)
                            local previous="" arg
                            for arg in "$@"; do
                                if [ "$previous" = "--out" ]; then
                                    mkdir -p "$(dirname "$arg")"
                                    printf '{{}}\\n' > "$arg"
                                fi
                                previous="$arg"
                            done
                            return 0
                            ;;
                        *) return 1 ;;
                    esac
                }}
                {reap}

                reap agent-run-0 0 target "$results" /work member
                test -f "$sweep_dir/agent_0/terminal_validation.json"
                test -f "$sweep_dir/agent_0/campaign_state.json"
                grep -q '^target=BANKED_PARTIAL$' "$sweep_dir/ledger"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                text=True,
                capture_output=True,
                env={**os.environ, "LC_ALL": "C"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "MARKER idx=0 target=target rc=1 end_reason=LIMIT", result.stdout
        )

    def test_exit_cleanup_threads_original_status_to_usage_audit(self):
        source = RUN_AGENTS.read_text()
        self.assertIn("local launcher_rc=\"${1:-0}\" agent_dir", source)
        self.assertIn(
            '_finalize_profile_audit "$agent_dir" "$launcher_rc"', source
        )
        self.assertIn('trap \'_cleanup_all "$?"\' EXIT', source)

    def test_trusted_resume_allows_only_receipted_generated_cargo_lock(self):
        source = RUN_AGENTS.read_text()
        self.assertIn(
            'reusable_seed_authority(receipt)["tree_hash"]', source,
        )
        self.assertIn(
            'else accepted_promotion_tree_hash(receipt)', source,
        )
        profile_start = source.index(
            "# A scored profile preserves the clean canonical root receipt"
        )
        profile_end = source.index(
            "# 4) assemble the in-container run.py argv", profile_start
        )
        profile_replay = source[profile_start:profile_end]
        self.assertIn('editable.add("Cargo.lock")', profile_replay)
        self.assertIn(
            '[ "$actual_profile_seed_tree" = "$SEED_RECEIPT_TREE_HASH" ]',
            profile_replay,
        )
        ordinary_seed = source[
            source.index("# 1c) optional RESUME seed"):
            source.index("# 2) seal the peeled tree")
        ]
        self.assertNotIn('editable.add("Cargo.lock")', ordinary_seed)

    def test_scored_launcher_binds_and_forwards_agent_turn_cap(self):
        source = RUN_AGENTS.read_text()
        self.assertIn("AGENT_MAX_TURNS=50", source)
        self.assertIn(
            '--agent-max-turns) AGENT_MAX_TURNS="$2"; shift 2 ;;',
            source,
        )
        self.assertIn('"agent_max_turns",', source)
        self.assertIn(
            '"agent_max_turns": int(sys.argv[8])',
            source,
        )
        self.assertIn(
            '--agent-max-turns "$AGENT_MAX_TURNS"',
            source,
        )

    def test_harness_image_uses_supported_node_22_runtime(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
        self.assertIn("FROM node:22-bookworm-slim AS node", dockerfile)
        self.assertIn("COPY --from=node /usr/local/ /usr/local/", dockerfile)
        self.assertNotIn("nodejs npm", dockerfile)
        self.assertIn("trusted_core_profile.py", dockerfile)
        self.assertIn("strip_specs.py", dockerfile)
        self.assertIn("COPY docker/ ./docker/", dockerfile)


if __name__ == "__main__":
    unittest.main()
