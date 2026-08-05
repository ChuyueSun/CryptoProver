# Trust Core campaign — final result record (2026-08-04)

**Outcome: COMPLETE, reproducible, and cleanly stopped.**

The runner-owned whole-crate Verus gate accepted terminal tree `978872a3850e74067068e8f3ff7375266740b83da37f1f8eaf6ca0cb2ef009c3` with 1,826 verified checks and zero verification, resource-limit, or raw Verus errors. The tree has zero hard admits. Forbidden-construct, frozen-definition, new-axiom, tooling-drift, and specification-drift inventories were empty. The supervisor then recorded `COMPLETE`, stopped without a successor, and left no campaign containers or network.

The terminal receipt's configured inventory reports 27 intentional axioms in its scored scope. A later independent crate-wide census found a broader fixed trust base: 50 pre-existing `axiom_*` functions (48 containing `admit()` and 2 containing `assume()`), 91 pre-existing `#[verifier::external_body]` attribute sites, 6 `assume_specification` declarations, and 6 `#[verifier::external]` functions outside the verified surface. None is among the agent-added forbidden constructs, and all 48 crate-wide admits occur inside `axiom_*` functions. Thus “1,826 verified, zero errors” is a Verus result for the checked surface relative to the fixed specifications, axioms, `vstd`, and toolchain—not an axiom-free result or a verification claim about excluded bodies.

## Result vector

| Boundary | Tree | Verification | Rlimit | Raw | Verified |
|---|---|---:|---:|---:|---:|
| Promoted start | `7905346e6cbba116bb60856fc3e5b62de0a74b0090ba088a2fff73c8868c33a9` | 83 | 8 | 92 | 1,527 |
| Accepted terminal | `978872a3850e74067068e8f3ff7375266740b83da37f1f8eaf6ca0cb2ef009c3` | 0 | 0 | 0 | 1,826 |

The terminal run was `tcv14-000003`, used three rounds, and lasted 5,198.193452 seconds. All observed model metadata named the registered `claude-fable-5` model; no fallback was credited.

## Independent green executions

The archived terminal tree passed four reviewed executions:

1. The round-3 runner gate.
2. A fresh, uncached terminal gate.
3. An isolated networkless replay with the completed workspace mounted read-only.
4. A clean reconstruction from the pinned Git baseline, cumulative patch, and separately sealed ignored `Cargo.lock`, followed by another networkless read-only replay.

The fourth execution established the compact provenance equation:

```text
baseline 4caeec90e06e53f1ca6b14980f64745caaf85868
+ cumulative.patch c2628476042f4faf90095753e8438dfaca74ff317df8b97d79bc640f219a5370
+ Cargo.lock       a25c704a61ce25682ee2b8bc27d476c911eea4ee6ac3e5c76809bfcb0c30f45f
= 726-file tree    978872a3850e74067068e8f3ff7375266740b83da37f1f8eaf6ca0cb2ef009c3
```

The binary patch is 1,245,606 bytes and modifies exactly 73 tracked paths. The reconstruction replay record has SHA-256 `28b941cc8cd453447f47f97b0b9bdec3cab065238ebcaf197d372addccaaa5c6` and reports return code 0, nontruncated output, 1,826 verified checks, and zero errors.

## Primary receipt identities

| Artifact | SHA-256 |
|---|---|
| Terminal `result.json` | `3567bb57df0ea16f2c06c619415596be6b359a233c2c46b568bc5c4205a99a96` |
| Promotion receipt | `c30a08568c5003cfbec2f98961dcc08b44a1f261dca84f483e00b19f548f9c9d` |
| Fresh terminal gate | `4c1cc8d605cfb6056e5149131761a74eeba8ca89e84fef1050c6f4f330dc3f20` |
| Third green replay | `21db7b6e56172b8b5073aaceed7de1a465d8ea819a1997ba081514b2c571e964` |
| Fourth green reconstruction replay | `28b941cc8cd453447f47f97b0b9bdec3cab065238ebcaf197d372addccaaa5c6` |
| Supervisor terminal state | `f15e0e7f6a1403010166885d754c4ffa6fe643f3541a2930523a6e1de72a5b92` |
| Seven-line supervisor ledger | `6fc3ad860ed14e9b4562bbe5e37f84335e658c17ce65e9f28936683db71d40f9` |
| Evidence archive | `eaa79428a6e8f9fe629ac0a2368e661479033fb22557815984a292dbb8c26845` |

The byte-identical evidence archive is `artifacts/trust_core_final_20260804.tar.gz`. It contains selected primary receipts, the patch and lockfile, an internal `SHA256SUMS`, and the reconstruction instructions available at archive time. Two later read-only, networkless audits reproduced the same `1,826 / 0` result; those post-archive executions are independently signed in the campaign review ledger but are not represented as durable receipts inside this archive.

## Accounting boundary

Cost is the only unresolved result claim. The terminal run records `$36.9268345`, and the visible campaign records sum to `$1166.7791045`, but both are lower bounds rather than exact totals because at least three upstream responses lack receipts. The mechanical `complete` fields in the archived usage audit and terminal validation cover only the receipted subset and must not be cited as an exact campaign-cost certificate.

## Reproduction

Extract the archive and first run its `verify_checksums.sh`. The baseline named by the archive, `4caeec90e06e53f1ca6b14980f64745caaf85868`, is a parentless synthetic peel seal rather than a public Git commit. Its contents were independently shown to equal the deterministic peel of public source ref `corefloor-base-103b92b9` at commit `103b92b9ddd93a6a904f7c86a48ec911cf533717`, using `peel_manifests/trusted_core_floor.json` (SHA-256 `06b825104529cd07e2131aa811ca85d6c5cdb203409337fc608cd5042576e9f7`). A fully public replay must recreate that peel with the campaign-pinned `peel.py`; the present archive does not itself record all three of those source/manifest/tool pins and therefore is not yet a standalone public reconstruction bundle.

From the recreated peel, run `git apply --check` and then apply `reconstruction/cumulative.patch`, and copy `reconstruction/Cargo.lock` to the repository root. Require the canonical 726-file source receipt to equal the terminal tree before running the registered gate.

Use verifier image ID `sha256:062b832a08f9740d9865593d82240c5deb2c64fb8a78ab80f956dcba2272fed2`, with networking disabled, the reconstructed source mounted read-only at `/work`, and a fresh writable Cargo target directory:

```sh
/usr/bin/python3 /opt/harness/skills/verus_check.py \
  /work/curve25519-dalek/src/ristretto.rs \
  --project /work/curve25519-dalek \
  --whole-crate --timeout 900 --rlimit 80.0
```

Scientific credit requires return code 0, nontruncated output, 1,826 verified checks, and zero verification, resource-limit, and raw Verus errors.
