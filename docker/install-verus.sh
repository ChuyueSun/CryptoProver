#!/usr/bin/env bash
# Provision Verus (the `verus` binary + the `cargo verus` subcommand) into /opt/verus.
#
# This is a HOOK, not a pinned download: the exact known-good Verus build differs
# per environment (memory:run-env-setup notes verus/rustc are not on PATH by default
# on these hosts). Point VERUS_TARBALL_URL at the SAME release the GCP VM already
# runs so bake-time == run-time, keeping the warm caches valid (T112 fingerprint
# concern). If you provision Verus another way (build from source, copy from the VM),
# replace the body below — the only contract is: after this script, `verus --version`
# and `cargo verus --help` both work and /opt/verus is on PATH.
set -euo pipefail

dest=/opt/verus
mkdir -p "$dest"

if [[ -z "${VERUS_TARBALL_URL:-}" ]]; then
    echo "install-verus.sh: VERUS_TARBALL_URL is empty." >&2
    echo "  Pass --build-arg VERUS_TARBALL_URL=<verus-x86-linux.tar.gz> pointing at" >&2
    echo "  the SAME Verus build the VM uses, or edit this script to copy it in." >&2
    exit 1
fi

echo "install-verus.sh: fetching $VERUS_TARBALL_URL"
curl -fsSL "$VERUS_TARBALL_URL" -o /tmp/verus.tar.gz
tar -xzf /tmp/verus.tar.gz -C "$dest" --strip-components=1
rm -f /tmp/verus.tar.gz

# `cargo verus` is a cargo subcommand: a `cargo-verus` shim must be on PATH.
if [[ ! -e "$dest/cargo-verus" && -e "$dest/cargo-verus.sh" ]]; then
    ln -sf "$dest/cargo-verus.sh" "$dest/cargo-verus"
fi

export PATH="$dest:$PATH"
verus --version
cargo verus --help >/dev/null
echo "install-verus.sh: OK"
