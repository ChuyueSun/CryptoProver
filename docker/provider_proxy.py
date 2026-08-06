#!/usr/bin/env python3
"""Fixed-upstream reverse proxy for scoreable Trust Core containers.

The proof-agent container lives on an internal Docker network with no external
route.  This sidecar is attached to both that network and an egress network,
but it is not a general forward proxy: request authority is ignored, CONNECT
is unsupported, paths are allowlisted, redirects are returned rather than
followed, and every upstream connection is pinned to the policy host.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import ssl
import socket
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any


HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    try:
        policy = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid proxy policy: {path}") from exc
    required = {
        "schema_version", "policy_id", "upstream_scheme", "upstream_host",
        "upstream_port", "allowed_methods", "allowed_path_prefixes",
        "maximum_request_bytes", "follow_redirects", "forward_proxy_connect",
    }
    missing = sorted(required - set(policy))
    if missing:
        raise ValueError(f"proxy policy missing: {', '.join(missing)}")
    if policy["schema_version"] != 1:
        raise ValueError("proxy policy schema_version must be 1")
    if policy["upstream_scheme"] != "https":
        raise ValueError("scoreable provider proxy requires HTTPS upstream")
    if policy["follow_redirects"] is not False:
        raise ValueError("scoreable provider proxy must not follow redirects")
    if policy["forward_proxy_connect"] is not False:
        raise ValueError("scoreable provider proxy must reject CONNECT")
    host = policy["upstream_host"]
    if not isinstance(host, str) or not host or "/" in host or ":" in host:
        raise ValueError("proxy policy upstream_host must be one DNS hostname")
    prefixes = policy["allowed_path_prefixes"]
    if not isinstance(prefixes, list) or not prefixes:
        raise ValueError("proxy policy needs non-empty allowed_path_prefixes")
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix.startswith("/") or prefix == "/":
            raise ValueError(f"unsafe proxy path prefix: {prefix!r}")
    digest = hashlib.sha256(canonical_json_bytes(policy)).hexdigest()
    return policy, digest


def clean_target(raw_target: str) -> str | None:
    """Return a safe origin-form path+query, or None for proxy/path tricks."""
    if not raw_target.startswith("/") or raw_target.startswith("//"):
        return None
    parsed = urllib.parse.urlsplit(raw_target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    decoded_path = urllib.parse.unquote(parsed.path)
    if any(part == ".." for part in decoded_path.split("/")):
        return None
    normalized = parsed.path or "/"
    return normalized + (f"?{parsed.query}" if parsed.query else "")


def path_allowed(target: str, prefixes: list[str]) -> bool:
    clean = target.split("?", 1)[0].rstrip("/")
    return any(
        clean == prefix
        or clean.startswith(prefix + "/")
        or clean.startswith(prefix + ":")
        for prefix in prefixes
    )


def filtered_headers(
    headers: http.client.HTTPMessage,
    *,
    upstream_host: str,
    body_size: int,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_HEADERS or lower in {"host", "content-length"}:
            continue
        result[key] = value
    result["Host"] = upstream_host
    result["Content-Length"] = str(body_size)
    result["Connection"] = "close"
    return result


def upstream_tls_ready(policy: dict[str, Any]) -> bool:
    """Prove the sidecar can reach only its fixed provider upstream."""
    host = policy["upstream_host"]
    port = int(policy["upstream_port"])
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return True
    except OSError:
        return False


def _log_line(payload: dict[str, Any], *, stderr: bool = False) -> None:
    """One os-level write per event line.

    print() under ThreadingHTTPServer performs two writes (payload, newline);
    concurrent handler threads can interleave them and corrupt the
    line-oriented accounting stream (T315).
    """
    stream = sys.stderr if stderr else sys.stdout
    stream.write(json.dumps(payload, sort_keys=True) + "\n")
    stream.flush()


class FixedProviderProxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TrustCoreProviderProxy/1"
    # A stalled client must not pin a daemon thread forever on a socket
    # read/write; sits just above the 900s upstream budget.
    timeout = 930

    @property
    def policy(self) -> dict[str, Any]:
        return self.server.policy  # type: ignore[attr-defined]

    @property
    def policy_sha256(self) -> str:
        return self.server.policy_sha256  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = canonical_json_bytes(value) + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {
                "okay": True,
                "policy_id": self.policy["policy_id"],
                "policy_sha256": self.policy_sha256,
            })
        elif self.path == "/readyz":
            ready = upstream_tls_ready(self.policy)
            self._json(200 if ready else 503, {
                "okay": ready,
                "policy_id": self.policy["policy_id"],
                "policy_sha256": self.policy_sha256,
                "upstream_tls": ready,
            })
        else:
            self._json(405, {"okay": False, "error": "method not allowed"})

    def do_CONNECT(self) -> None:  # noqa: N802
        self._json(405, {"okay": False, "error": "CONNECT disabled"})

    def _deny(self, status: int, reason: str) -> None:
        # Denials never reach upstream (no billing), but a silent denial is
        # an audit hole: request counts on the CLI side could not be
        # distinguished from "never reached the proxy" (T315).
        _log_line({
            "event": "request_denied",
            "status": status,
            "reason": reason,
            "path": (clean_target(self.path) or self.path).split("?", 1)[0],
        }, stderr=True)
        try:
            self._json(status, {"okay": False, "error": reason})
        except OSError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        target = clean_target(self.path)
        allowed_methods = self.policy["allowed_methods"]
        if "POST" not in allowed_methods:
            self._deny(405, "method not allowed")
            return
        if target is None or not path_allowed(
            target, self.policy["allowed_path_prefixes"],
        ):
            self._deny(404, "path not allowed")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._deny(400, "invalid content length")
            return
        maximum = int(self.policy["maximum_request_bytes"])
        if length < 0 or length > maximum:
            self._deny(413, "request too large")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._deny(400, "truncated request")
            return

        host = self.policy["upstream_host"]
        port = int(self.policy["upstream_port"])
        started = time.time()
        # Receipt discipline (T315, three production receipt holes): the
        # provider_request line lives in a `finally` so every request that
        # opens an upstream attempt prints exactly one accounting line no
        # matter which side of the connection dies; client disconnects are
        # their own structured event carrying the TRUE upstream status (a
        # billed 200 must not masquerade as 502); the 502 reply is skipped
        # once headers are on the wire (writing a second status line into a
        # half-sent body corrupts the client stream) and its write failure is
        # swallowed; the upstream connection is always closed promptly so the
        # provider can cancel generation instead of streaming into the void.
        connection = None
        status = 502
        client_error: str | None = None
        headers_sent = False
        try:
            connection = http.client.HTTPSConnection(
                host, port, timeout=900, context=ssl.create_default_context(),
            )
            connection.request(
                "POST",
                target,
                body=body,
                headers=filtered_headers(
                    self.headers, upstream_host=host, body_size=len(body),
                ),
            )
            response = connection.getresponse()
            status = response.status
            try:
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    lower = key.lower()
                    if lower in HOP_HEADERS or lower in {"content-length", "connection"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
            except OSError as exc:
                client_error = type(exc).__name__
            while client_error is None:
                # read1: return as soon as ANY data is available. read(64k)
                # accumulates the full 64KB across chunks, holding SSE events
                # back for minutes during long generation pauses — plausibly
                # the root cause of the observed client resets.
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError as exc:
                    client_error = type(exc).__name__
            self.close_connection = True
        except Exception as exc:
            # Only claim "upstream unavailable" when we never got a real
            # upstream status. A failure raised by response.read1() AFTER
            # getresponse() succeeded is a mid-stream death on an already
            # BILLED response: overwriting its 200 with 502 recreates, on the
            # upstream side, exactly the masquerade this block fixes for
            # client-side deaths. (Pre-response, status is already 502 from
            # the initializer, so this is the only case that assigns.)
            if not headers_sent:
                status = 502
            _log_line({
                "event": "upstream_error",
                "type": type(exc).__name__,
                "path": target.split("?", 1)[0],
            }, stderr=True)
            if not headers_sent:
                try:
                    self._json(502, {"okay": False, "error": "fixed upstream unavailable"})
                except OSError:
                    pass
            self.close_connection = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if client_error is not None:
                _log_line({
                    "event": "client_disconnect",
                    "type": client_error,
                    "path": target.split("?", 1)[0],
                    "upstream_status": status,
                }, stderr=True)
            _log_line({
                "event": "provider_request",
                "method": "POST",
                "path": target.split("?", 1)[0],
                "status": status,
                "client_disconnect": client_error is not None,
                "duration_ms": round((time.time() - started) * 1000),
                "policy_sha256": self.policy_sha256,
            })


class ThreadingProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    try:
        policy, digest = load_policy(args.policy)
    except ValueError as exc:
        parser.error(str(exc))
    server = ThreadingProxyServer((args.host, args.port), FixedProviderProxy)
    server.policy = policy
    server.policy_sha256 = digest
    print(json.dumps({
        "event": "provider_proxy_ready",
        "listen": f"{args.host}:{args.port}",
        "policy_id": policy["policy_id"],
        "policy_sha256": digest,
        "upstream": policy["upstream_host"],
    }, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
