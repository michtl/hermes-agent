"""Shared bounded budgets for one owner-bound Relay hard-stop.

The transport timeout is intentionally derived from every bounded inner phase.
Keeping the arithmetic here prevents a future local timeout change from making
the outer acknowledgement cap cancel a normally progressing terminal Stop.
"""

HARD_STOP_REAP_TIMEOUT_SECONDS = 4.0
INTERRUPT_ACTIVITY_TIMEOUT_SECONDS = 1.0
SESSION_PROCESSING_CANCEL_TIMEOUT_SECONDS = 5.0

# Scheduler/logging/queue overhead after the three sequential inner phases.
INTERRUPT_HANDLER_SAFETY_MARGIN_SECONDS = 1.0
# Strictly greater than inner phases plus the documented safety margin.
INTERRUPT_HANDLER_TIMEOUT_SECONDS = (
    HARD_STOP_REAP_TIMEOUT_SECONDS
    + INTERRUPT_ACTIVITY_TIMEOUT_SECONDS
    + SESSION_PROCESSING_CANCEL_TIMEOUT_SECONDS
    + INTERRUPT_HANDLER_SAFETY_MARGIN_SECONDS
    + 0.5
)

# Independent last-resort guard for a lost barrier signal. It exceeds the
# transport cap so the ordinary canonical Stop remains authoritative; only a
# broken/cancelled signal reaches this fail-safe.
SESSION_HANDOFF_BARRIER_TIMEOUT_SECONDS = (
    INTERRUPT_HANDLER_TIMEOUT_SECONDS + 0.5
)
