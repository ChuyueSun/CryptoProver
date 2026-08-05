#!/usr/bin/env python3
"""Fail-closed in-container sterility and network-oracle probe."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

PUBLIC_EGRESS_PROBE_IP = "1.1.1.1"


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, text=True, capture_output=True, timeout=30,
    )


def _tcp_reachable(host: str, port: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=4):
            return True, "connected"
    except OSError as exc:
        return False, type(exc).__name__


def _proxy_json(host: str, port: int, method: str, path: str) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection(host, port, timeout=15)
    connection.request(method, path, body=b"{}" if method == "POST" else None)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        value = {"raw_sha256": hashlib.sha256(body).hexdigest()}
    return response.status, value


def _git_seal_checks(work: Path) -> dict[str, Any]:
    git = ["git", "-c", f"safe.directory={work}"]
    parent = _run([*git, "rev-parse", "-q", "--verify", "HEAD^"], cwd=work)
    refs = _run([
        *git, "for-each-ref", "--format=%(refname)",
        "refs/heads", "refs/remotes", "refs/tags",
    ], cwd=work)
    remotes = _run([*git, "remote"], cwd=work)
    fsck = _run([
        *git, "fsck", "--no-reflogs", "--unreachable", "--no-progress",
    ], cwd=work)
    return {
        "head_has_parent": parent.returncode == 0,
        "refs": [line for line in refs.stdout.splitlines() if line],
        "remotes": [line for line in remotes.stdout.splitlines() if line],
        "fsck_output": (fsck.stdout + fsck.stderr).strip(),
        "okay": (
            parent.returncode != 0
            and not refs.stdout.strip()
            and not remotes.stdout.strip()
            and not (fsck.stdout + fsck.stderr).strip()
        ),
    }


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _source_oracle_paths() -> list[str]:
    roots = [Path("/opt"), Path("/root"), Path("/home"), Path("/usr/local")]
    forbidden: list[str] = []
    allowed_roots = (Path("/opt/harness"),)
    for root in roots:
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            if any(_path_is_within(current_path, allowed) for allowed in allowed_roots):
                dirs[:] = []
                continue
            dirs[:] = [
                name for name in dirs
                if name not in {"target", "__pycache__", "node_modules"}
            ]
            for name in list(dirs):
                lowered = name.lower()
                if name == "dalek-lite" or lowered.startswith("curve25519-dalek"):
                    forbidden.append(str(current_path / name))
            for name in files:
                if not name.endswith(".rs"):
                    continue
                path = current_path / name
                try:
                    text = path.read_text(errors="ignore")
                except OSError:
                    continue
                if "proof fn lemma_five_limbs_equals_to_nat" in text:
                    forbidden.append(str(path))
    return sorted(set(forbidden))


def probe(
    *,
    work: Path,
    proxy_host: str,
    proxy_port: int,
    expected_policy_sha256: str,
) -> dict[str, Any]:
    github_reachable, github_detail = _tcp_reachable("github.com", 443)
    public_ip_reachable, public_ip_detail = _tcp_reachable(
        PUBLIC_EGRESS_PROBE_IP, 443,
    )
    health_status, health = _proxy_json(proxy_host, proxy_port, "GET", "/healthz")
    ready_status, ready = _proxy_json(proxy_host, proxy_port, "GET", "/readyz")
    fetch_status, fetch = _proxy_json(
        proxy_host, proxy_port, "POST",
        "/fetch?url=https://github.com/ChuyueSun/dalek-lite",
    )
    git_seal = _git_seal_checks(work)
    source_paths = _source_oracle_paths()
    mountinfo = Path("/proc/self/mountinfo").read_text(errors="replace")
    docker_socket = Path("/var/run/docker.sock").exists()
    warm_source = Path("/opt/warm-src").exists()
    policy_matches = (
        health.get("policy_sha256") == expected_policy_sha256
        and ready.get("policy_sha256") == expected_policy_sha256
    )
    checks = {
        "general_github_tcp_blocked": not github_reachable,
        "general_public_ip_tcp_blocked": not public_ip_reachable,
        "provider_proxy_health": health_status == 200 and health.get("okay") is True,
        "provider_proxy_tls_ready": ready_status == 200 and ready.get("upstream_tls") is True,
        "provider_proxy_policy_matches": policy_matches,
        "provider_proxy_rejects_fetch": fetch_status == 404,
        "isolated_git_store": git_seal["okay"],
        "no_docker_socket": not docker_socket,
        "no_warm_source_tree": not warm_source,
        "no_reference_source_paths": not source_paths,
        "work_and_results_mounted": "/work" in mountinfo and "/results" in mountinfo,
    }
    return {
        "schema_version": 1,
        "kind": "in_container_sterility_probe",
        "okay": all(checks.values()),
        "checks": checks,
        "network": {
            "github_reachable": github_reachable,
            "github_detail": github_detail,
            "public_ip_probe": PUBLIC_EGRESS_PROBE_IP,
            "public_ip_reachable": public_ip_reachable,
            "public_ip_detail": public_ip_detail,
            "proxy_health_status": health_status,
            "proxy_ready_status": ready_status,
            "proxy_fetch_status": fetch_status,
            "proxy_fetch_response": fetch,
        },
        "filesystem": {
            "git_seal": git_seal,
            "docker_socket": docker_socket,
            "warm_source_tree": warm_source,
            "reference_source_paths": source_paths,
        },
        "provider_policy_sha256": expected_policy_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=Path("/work"))
    parser.add_argument("--proxy-host", default="provider-proxy")
    parser.add_argument("--proxy-port", type=int, default=8080)
    parser.add_argument("--policy-sha256", required=True)
    args = parser.parse_args()
    try:
        receipt = probe(
            work=args.work,
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            expected_policy_sha256=args.policy_sha256,
        )
    except Exception as exc:
        receipt = {
            "schema_version": 1,
            "kind": "in_container_sterility_probe",
            "okay": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("okay") else 1


if __name__ == "__main__":
    raise SystemExit(main())
