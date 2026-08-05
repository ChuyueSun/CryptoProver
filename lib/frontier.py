"""The registered partial-frontier ordering — the single production body.

Consumed by run.py (round transaction telemetry + best-decided frontier
guard + terminal bank dominance) and by docker/trusted_core_next_package.py
(the successor generator's independent re-validation). Both import THIS
function; receipt-shape strictness stays local to each boundary. Keeping
one body is what prevents the drift class that produced the generator's
INITIAL-gap stall (F2, 2026-08-03).
"""

from __future__ import annotations

VECTOR_SECONDARY_KEYS = (
    "resource_limits", "timeouts", "panics", "build_wrappers", "compile_errors",
)


def vector_relation(
    previous_vector: dict, current_vector: dict, *,
    previous_tree_hash: str = "", current_tree_hash: str = "",
    missing_previous: str = "equal",
) -> str:
    """Classify ``current_vector`` against ``previous_vector``.

    Returns IMPROVED / DISPLACED / REGRESSED / NEUTRAL over ``_gate_vector``
    dicts. Verification errors are the primary metric — an increase is always
    REGRESSED even when secondary counters improve. ``missing_previous`` picks
    the absent-previous-key semantics: "equal" (transaction telemetry — an
    absent key can never regress) or "zero" (frontier receipts — decided
    gates always carry full vectors).
    """
    if current_tree_hash and current_tree_hash == previous_tree_hash:
        return "NEUTRAL"
    current_verification = int(current_vector.get("verification_errors") or 0)
    if missing_previous == "equal":
        previous_verification = int(
            previous_vector.get("verification_errors", current_verification)
            or 0
        )
    else:
        previous_verification = int(
            previous_vector.get("verification_errors") or 0
        )
    if current_verification < previous_verification:
        return "IMPROVED"
    if current_verification > previous_verification:
        return "REGRESSED"
    if any(
        int(current_vector.get(key) or 0) > int(previous_vector.get(key) or 0)
        for key in VECTOR_SECONDARY_KEYS
    ):
        return "DISPLACED"
    current_raw = int(current_vector.get("raw_errors") or 0)
    if missing_previous == "equal":
        previous_raw = int(previous_vector.get("raw_errors", current_raw) or 0)
    else:
        previous_raw = int(previous_vector.get("raw_errors") or 0)
    current_verified = current_vector.get("verified_count")
    previous_verified = previous_vector.get("verified_count")
    if current_raw > previous_raw or (
        current_verified is not None
        and previous_verified is not None
        and int(current_verified) < int(previous_verified)
    ):
        return "REGRESSED"
    return "NEUTRAL"
