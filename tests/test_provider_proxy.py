"""Security-boundary tests for the fixed-upstream Trust Core proxy."""
from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from docker.provider_proxy import (  # noqa: E402
    FixedProviderProxy,
    clean_target,
    filtered_headers,
    load_policy,
    path_allowed,
)
from docker.sterility_probe import (  # noqa: E402
    PUBLIC_EGRESS_PROBE_IP,
    _git_seal_checks,
    _path_is_within,
)


class ProviderProxyPolicyTests(unittest.TestCase):
    def test_repository_policy_is_fixed_https_anthropic(self):
        policy, digest = load_policy(
            REPO_ROOT / "docker" / "provider_proxy_policy.json",
        )
        self.assertEqual(policy["upstream_scheme"], "https")
        self.assertEqual(policy["upstream_host"], "api.anthropic.com")
        self.assertFalse(policy["follow_redirects"])
        self.assertFalse(policy["forward_proxy_connect"])
        self.assertEqual(len(digest), 64)

    def test_absolute_urls_authority_tricks_and_traversal_are_rejected(self):
        for target in (
            "https://github.com/ChuyueSun/dalek-lite",
            "//github.com/repo",
            "/v1/messages/../fetch",
            "/v1/messages/%2e%2e/fetch",
        ):
            with self.subTest(target=target):
                self.assertIsNone(clean_target(target))

    def test_only_registered_provider_paths_are_allowed(self):
        prefixes = ["/v1/messages", "/v1/complete"]
        self.assertTrue(path_allowed("/v1/messages?beta=true", prefixes))
        self.assertTrue(path_allowed("/v1/messages/count_tokens", prefixes))
        self.assertFalse(path_allowed("/fetch?url=https://github.com", prefixes))
        self.assertFalse(path_allowed("/", prefixes))

    def test_request_authority_is_overridden_and_proxy_headers_removed(self):
        headers = Message()
        headers["Host"] = "github.com"
        headers["Content-Length"] = "999"
        headers["Proxy-Authorization"] = "secret"
        headers["Authorization"] = "Bearer provider-token"
        result = filtered_headers(
            headers, upstream_host="api.anthropic.com", body_size=7,
        )
        self.assertEqual(result["Host"], "api.anthropic.com")
        self.assertEqual(result["Content-Length"], "7")
        self.assertNotIn("Proxy-Authorization", result)
        self.assertEqual(result["Authorization"], "Bearer provider-token")

    def test_policy_rejects_general_forward_proxy_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            policy_path = Path(td) / "policy.json"
            policy = json.loads(
                (REPO_ROOT / "docker" / "provider_proxy_policy.json").read_text(),
            )
            policy["forward_proxy_connect"] = True
            policy_path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(ValueError, "reject CONNECT"):
                load_policy(policy_path)

    def test_upstream_stream_failure_does_not_emit_second_status_line(self):
        class Response:
            status = 200
            reason = "OK"

            @staticmethod
            def getheaders():
                return []

            @staticmethod
            def read(_size):
                raise RuntimeError("upstream truncated")

        connection = mock.Mock()
        connection.getresponse.return_value = Response()
        handler = FixedProviderProxy.__new__(FixedProviderProxy)
        handler.path = "/v1/messages"
        handler.command = "POST"
        handler.requestline = "POST /v1/messages HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = False
        handler.headers = Message()
        handler.headers["Content-Length"] = "2"
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.server = SimpleNamespace(
            policy={
                "allowed_methods": ["POST"],
                "allowed_path_prefixes": ["/v1/messages"],
                "maximum_request_bytes": 1024,
                "upstream_host": "api.anthropic.com",
                "upstream_port": 443,
            },
            policy_sha256="a" * 64,
        )
        with mock.patch(
            "docker.provider_proxy.http.client.HTTPSConnection",
            return_value=connection,
        ):
            handler.do_POST()
        wire = handler.wfile.getvalue()
        self.assertEqual(wire.count(b"HTTP/1.1"), 1)
        self.assertNotIn(b"502", wire)
        self.assertTrue(handler.close_connection)

    def test_git_seal_probe_distinguishes_history_from_orphan_root(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "file").write_text("one")
            subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
            commit = [
                "git", "-C", str(repo),
                "-c", "user.email=test@example.com",
                "-c", "user.name=test", "commit", "-q", "-m", "root",
            ]
            subprocess.run(commit, check=True)
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "-q", "--detach"],
                check=True,
            )
            refs = subprocess.run(
                [
                    "git", "-C", str(repo), "for-each-ref",
                    "--format=%(refname)", "refs/heads",
                ],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            for ref in refs:
                subprocess.run(
                    ["git", "-C", str(repo), "update-ref", "-d", ref],
                    check=True,
                )
            self.assertTrue(_git_seal_checks(repo)["okay"])

            (repo / "file").write_text("two")
            subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
            subprocess.run(commit[:-1] + ["child"], check=True)
            self.assertFalse(_git_seal_checks(repo)["okay"])

    def test_direct_ip_probe_is_not_dns_dependent(self):
        octets = PUBLIC_EGRESS_PROBE_IP.split(".")
        self.assertEqual(len(octets), 4)
        self.assertTrue(all(part.isdigit() for part in octets))

    def test_harness_allow_root_does_not_exempt_prefix_sibling(self):
        self.assertTrue(
            _path_is_within(
                Path("/opt/harness/docker"),
                Path("/opt/harness"),
            ),
        )
        self.assertFalse(
            _path_is_within(
                Path("/opt/harness_gt/curve25519-dalek"),
                Path("/opt/harness"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
