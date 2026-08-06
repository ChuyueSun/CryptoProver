"""Security-boundary tests for the fixed-upstream Trust Core proxy."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from docker.provider_proxy import (  # noqa: E402
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


class ProviderProxyStreamingReceipts(unittest.TestCase):
    """The accounting stream, exercised rather than assumed.

    Every unreceipted provider response is money spent with no local record,
    so these pin the properties that make the receipt trustworthy: exactly one
    accounting line per upstream attempt, the TRUE upstream status even when a
    connection dies, client disconnects distinguished from upstream failures,
    incremental streaming, and the upstream connection always closed.
    """

    def _handler(self, *, response=None, raise_on_read=None,
                 raise_on_write=None, raise_before_response=None):
        """A do_POST bound to fakes, capturing every emitted log line."""
        import docker.provider_proxy as proxy

        emitted: list[dict] = []
        closed: list[bool] = []

        class FakeResponse:
            status = 200
            reason = "OK"

            def __init__(self):
                self._chunks = list(response or [b"data: one\n", b"data: two\n"])

            def getheaders(self):
                return [("Content-Type", "text/event-stream")]

            def read1(self, _n):
                if raise_on_read is not None and self._chunks:
                    raise raise_on_read
                return self._chunks.pop(0) if self._chunks else b""

        class FakeConnection:
            def __init__(self, *a, **k):
                pass

            def request(self, *a, **k):
                if raise_before_response is not None:
                    raise raise_before_response

            def getresponse(self):
                return FakeResponse()

            def close(self):
                closed.append(True)

        class Handler(proxy.FixedProviderProxy):
            def __init__(self):  # bypass BaseHTTPRequestHandler's socket setup
                self.path = "/v1/messages"
                self.headers = Message()
                self.headers["Content-Length"] = "2"
                self.rfile = io.BytesIO(b"{}")
                self.wfile = io.BytesIO()
                self.close_connection = False

            @property
            def policy(self):
                return {
                    "allowed_methods": ["POST"],
                    "allowed_path_prefixes": ["/v1/messages"],
                    "maximum_request_bytes": 1000,
                    "upstream_host": "api.example.com",
                    "upstream_port": 443,
                }

            @property
            def policy_sha256(self):
                return "0" * 64

            def send_response(self, *a, **k):
                if raise_on_write is not None:
                    raise raise_on_write

            def send_header(self, *a, **k):
                pass

            def end_headers(self):
                pass

        original_conn = proxy.http.client.HTTPSConnection
        original_log = proxy._log_line
        proxy.http.client.HTTPSConnection = FakeConnection
        proxy._log_line = lambda payload, stderr=False: emitted.append(payload)
        try:
            Handler().do_POST()
        finally:
            proxy.http.client.HTTPSConnection = original_conn
            proxy._log_line = original_log
        return emitted, closed

    def _receipts(self, emitted):
        return [e for e in emitted if e["event"] == "provider_request"]

    def test_clean_stream_emits_exactly_one_receipt_with_true_status(self):
        emitted, closed = self._handler()
        receipts = self._receipts(emitted)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], 200)
        self.assertFalse(receipts[0]["client_disconnect"])
        self.assertTrue(closed, "upstream connection was not closed")

    def test_client_disconnect_keeps_true_upstream_status(self):
        # A billed 200 whose CLIENT vanished must not be recorded as a 502.
        emitted, closed = self._handler(raise_on_write=BrokenPipeError(32, "x"))
        receipts = self._receipts(emitted)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], 200)
        self.assertTrue(receipts[0]["client_disconnect"])
        self.assertEqual(
            [e["event"] for e in emitted if e["event"] == "client_disconnect"],
            ["client_disconnect"],
        )
        self.assertTrue(closed)

    def test_midstream_upstream_death_keeps_true_upstream_status(self):
        # The upstream-side twin: read1 raising AFTER a 200 was received is
        # still a BILLED response, so overwriting it with 502 would undercount.
        emitted, closed = self._handler(
            raise_on_read=ConnectionResetError(104, "reset"))
        receipts = self._receipts(emitted)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], 200)
        self.assertIn(
            "upstream_error", [e["event"] for e in emitted])
        self.assertTrue(closed)

    def test_pre_response_failure_receipts_502_exactly_once(self):
        emitted, closed = self._handler(
            raise_before_response=OSError("dns"))
        receipts = self._receipts(emitted)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], 502)
        self.assertTrue(closed)

    def test_streaming_uses_read1_so_events_are_not_buffered(self):
        # read(64k) accumulates across chunks and stalls SSE for minutes;
        # read1 returns as soon as any data is available.
        source = (REPO_ROOT / "docker" / "provider_proxy.py").read_text()
        self.assertIn("response.read1(", source)
        self.assertNotIn("response.read(64", source)

    def test_policy_denials_are_receipted(self):
        import docker.provider_proxy as proxy
        emitted: list[dict] = []
        original_log = proxy._log_line
        proxy._log_line = lambda payload, stderr=False: emitted.append(payload)
        try:
            class Handler(proxy.FixedProviderProxy):
                def __init__(self):
                    self.path = "/nope"
                    self.headers = Message()
                    self.rfile = io.BytesIO(b"")
                    self.wfile = io.BytesIO()
                    self.close_connection = False

                @property
                def policy(self):
                    return {
                        "allowed_methods": ["POST"],
                        "allowed_path_prefixes": ["/v1/messages"],
                        "maximum_request_bytes": 1000,
                        "upstream_host": "h", "upstream_port": 443,
                    }

                @property
                def policy_sha256(self):
                    return "0" * 64

                def send_response(self, *a, **k):
                    pass

                def send_header(self, *a, **k):
                    pass

                def end_headers(self):
                    pass

            Handler().do_POST()
        finally:
            proxy._log_line = original_log
        self.assertEqual(
            [e["event"] for e in emitted], ["request_denied"])
        self.assertEqual(emitted[0]["status"], 404)


if __name__ == "__main__":
    unittest.main()
