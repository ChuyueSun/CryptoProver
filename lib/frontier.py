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
    # Hard admits dominate everything: admit() makes Verus accept any
    # postcondition, so an admit increase must read REGRESSED even when it
    # erases verification errors — otherwise an admit-stuffed tree scores
    # IMPROVED and becomes the banked frontier.
    #
    # Compared ONLY when BOTH vectors carry the key. Mixed presence is real:
    # legacy banked receipts and any vector built before run.py started
    # emitting the key have no `hard_admits`, and coercing an absent previous
    # to 0 read a false REGRESSED against every honest bank that still had
    # admits in scope — destroying the leg and halting the chain. Absent on
    # either side means "unknown", and unknown must not decide the ordering.
    if "hard_admits" in previous_vector and "hard_admits" in current_vector:
        if int(current_vector["hard_admits"] or 0) > int(
            previous_vector["hard_admits"] or 0
        ):
            return "REGRESSED"
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
        # Erasing source verification errors while GROWING the solver-limit
        # set is displacement, not improvement — the same principle
        # _classify_candidate_transaction states for wrapper kinds. Without
        # this, 1-verification -> 0-verification+30-rlimit reads IMPROVED and
        # the terminal restore prefers the rlimit explosion (T315 M2).
        current_rlimits = int(current_vector.get("resource_limits") or 0)
        if missing_previous == "equal":
            previous_rlimits = int(
                previous_vector.get("resource_limits", current_rlimits) or 0
            )
        else:
            previous_rlimits = int(
                previous_vector.get("resource_limits") or 0
            )
        if current_rlimits > previous_rlimits:
            return "DISPLACED"
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
