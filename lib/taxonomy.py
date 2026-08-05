"""End-reason taxonomy — the single production definition.

Consumed by run.py (the _final_end_reason gate and run_task's terminal
disposition block) and by docker/trusted_core_supervisor.py (retry/stop
routing). Hand-copied string sets are the drift class that made the durable
supervisor stop forever on RATE_LIMIT_OR_HANG while carrying a label nothing
emits (F3, 2026-08-03); both consumers import THIS module and the parity
test asserts identity, not equality between copies.
"""

from __future__ import annotations

# Transient infrastructure: the round never really ran. Preserved ABOVE the
# done_for_real promotion; never promoted to COMPLETE.
INFRA_END_REASONS = (
    "RATE_LIMITED", "RATE_LIMIT_OR_HANG", "RETRY_EXHAUSTED", "TRANSPORT_ERROR",
    "USER_INTERRUPTED", "INTERRUPTED_SIGNAL", "PROCESS_CROSSTALK",
)

# Subset the durable supervisor may auto-retry without a human: excludes
# USER_INTERRUPTED / INTERRUPTED_SIGNAL (a human took control) and
# PROCESS_CROSSTALK (environment contamination needs a human look); adds the
# pre-provider PREMODEL_GATE_INDETERMINATE warm outcome, which never reaches
# the round loop.
AUTORETRY_END_REASONS = (
    "RATE_LIMITED", "RATE_LIMIT_OR_HANG", "RETRY_EXHAUSTED", "TRANSPORT_ERROR",
    "PREMODEL_GATE_INDETERMINATE",
)

# Cheat/terminal class: never promoted to COMPLETE even over a green final
# state (SIBLING_VERUS_FAIL is terminal here yet still bank-eligible below —
# an honest partial whose sibling breakage forbids a target-only COMPLETE).
CHEAT_END_REASONS = (
    "SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT", "SIBLING_VERUS_FAIL",
    "GIT_RECOVERY", "FROZEN_EDIT", "FORBIDDEN_CONSTRUCT",
)

# Terminal integrity rejections: restore last integrity-clean snapshot and
# reject the tree outright.
INTEGRITY_REJECTION_END_REASONS = (
    "SPEC_DRIFT", "AXIOM_DRIFT", "FORBIDDEN_CONSTRUCT", "FROZEN_EDIT",
    "GIT_RECOVERY", "PROCESS_CROSSTALK", "TOOLING_DRIFT",
)

# Honest partial terminals eligible for a reusable bank.
BANKABLE_END_REASONS = ("LIMIT", "NEEDS_DECOMP", "SIBLING_VERUS_FAIL")

# Terminal labels the durable supervisor treats as a DELIBERATE stop (ledger
# reason DECISION/STOP rather than unknown-terminal fail-close). COMPLETE is
# the campaign finishing; the rest are human-took-control or cheat-class
# labels whose evidence a human must review before anything relaunches.
STOP_END_REASONS = (
    "COMPLETE", "USER_INTERRUPTED", "SPEC_DRIFT", "AXIOM_DRIFT",
    "TOOLING_DRIFT", "FORBIDDEN_CONSTRUCT",
)
