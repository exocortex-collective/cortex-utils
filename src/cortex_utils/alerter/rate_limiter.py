"""Rate limiter for alert deduplication."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

# The cooldown below suppresses repeats, so a multi-day outage looks exactly
# like a single blip. These bound the "still happening" judgement; see
# is_sustained.
DEFAULT_ESCALATE_AFTER_MINUTES = 30
DEFAULT_ESCALATE_AFTER_OCCURRENCES = 3
# How long a key must be quiet before the next occurrence starts a NEW run,
# so a key seen in July and again in November is two events and not one.
DEFAULT_RUN_GAP_MINUTES = 60


@dataclass(frozen=True)
class Run:
    """One unbroken stretch of the same error_key firing.

    `escalated` is what makes one outage ping ONCE. Forgetting the run instead
    re-escalates every escalate_after_minutes for as long as the outage lasts.
    """

    started: datetime
    last: datetime
    count: int
    escalated: bool = False


def is_sustained(
    run: Run | None,
    now: datetime,
    escalate_after_minutes: int,
    escalate_after_occurrences: int,
) -> bool:
    """Has this run been going long enough, and often enough, to escalate?

    Both bounds must be met: duration alone
    would escalate a single error seen once an hour ago and never again, and
    count alone would escalate a burst of three lines in one second, which is
    one event rather than a sustained one.
    """
    if run is None or run.escalated:
        return False
    if run.count < escalate_after_occurrences:
        return False
    return (now - run.started) >= timedelta(minutes=escalate_after_minutes)


class RateLimiter:
    """Rate limiter to prevent alert spam.

    Tracks when each error_key was last alerted and enforces
    a cooldown period before allowing another alert.
    """

    def __init__(self):
        self.last_alert: dict[str, datetime] = {}
        self.warning_counts: dict[str, int] = defaultdict(int)
        # One representative message per key. The counts alone rendered a
        # summary of opaque digests -- "Unclassified:8A416200 (cortex-gateway):
        # 2" -- which told a reader nothing about what had failed.
        self.warning_samples: dict[str, str] = {}
        self.last_reset: datetime = datetime.now()
        # Independent of last_alert, deliberately: last_alert records when we
        # SPOKE, and the cooldown keeps us quiet, so it cannot answer "is this
        # still happening". These record what the container actually said.
        self.runs: dict[str, Run] = {}

    def should_alert(self, error_key: str, cooldown_minutes: int) -> bool:
        """Check if we should send an alert for this error.

        Args:
            error_key: Unique identifier for this error type
            cooldown_minutes: Minimum minutes between alerts (0 = always alert)

        Returns:
            True if we should alert, False if still in cooldown
        """
        if cooldown_minutes == 0:
            # No cooldown - always alert
            self.last_alert[error_key] = datetime.now()
            return True

        last = self.last_alert.get(error_key)
        now = datetime.now()

        if last is None or (now - last) >= timedelta(minutes=cooldown_minutes):
            self.last_alert[error_key] = now
            return True

        return False

    def note_occurrence(
        self, error_key: str, now: datetime | None = None, run_gap_minutes: int | None = None
    ) -> Run:
        """Record that this error_key just fired, and return its current run.

        Called on EVERY occurrence, including ones the cooldown then suppresses.
        """
        now = now or datetime.now()
        gap = DEFAULT_RUN_GAP_MINUTES if run_gap_minutes is None else run_gap_minutes
        prev = self.runs.get(error_key)
        if prev is None or (now - prev.last) > timedelta(minutes=gap):
            run = Run(started=now, last=now, count=1)
        else:
            run = Run(
                started=prev.started,
                last=now,
                count=prev.count + 1,
                escalated=prev.escalated,
            )
        self.runs[error_key] = run
        return run

    def mark_escalated(self, error_key: str) -> None:
        """Record that this run has already pinged, so it will not ping again.

        The run keeps accumulating -- the outage is still happening and the
        HIGH alerts keep posting on their own cooldown -- but it cannot
        re-escalate. Only a quiet gap longer than the run gap, which starts a
        fresh run, makes another ping possible.
        """
        prev = self.runs.get(error_key)
        if prev is not None:
            self.runs[error_key] = Run(prev.started, prev.last, prev.count, escalated=True)

    def increment_warning(self, error_key: str, sample: str = "") -> None:
        """Increment warning count for daily summary aggregation.

        `sample` is one representative message for this key, kept so the daily
        summary can say what failed rather than only how often. The FIRST is
        kept: every later occurrence shares the key, so they describe the same
        fault, and keeping the first avoids re-writing the dict on every line.
        """
        self.warning_counts[error_key] += 1
        if sample and error_key not in self.warning_samples:
            self.warning_samples[error_key] = sample

    def get_warning_counts(self) -> dict[str, int]:
        """Get accumulated warning counts since last reset."""
        return dict(self.warning_counts)

    def reset_warning_counts(self) -> dict[str, int]:
        """Reset warning counts and return the old values.

        Call this after sending daily summary.
        """
        counts = dict(self.warning_counts)
        self.warning_counts.clear()
        self.warning_samples.clear()
        self.last_reset = datetime.now()
        return counts

    def time_until_alert(self, error_key: str, cooldown_minutes: int) -> timedelta | None:
        """Get time remaining until this error can alert again.

        Returns:
            Time remaining, or None if can alert now
        """
        if cooldown_minutes == 0:
            return None

        last = self.last_alert.get(error_key)
        if last is None:
            return None

        elapsed = datetime.now() - last
        cooldown = timedelta(minutes=cooldown_minutes)

        if elapsed >= cooldown:
            return None

        return cooldown - elapsed
