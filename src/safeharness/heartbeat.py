"""Independent, bounded lease renewal; errors fence local execution permanently."""

import math
from threading import Event, Thread

from .database import engine_for
from .scheduling import renew_claim, valid_lease_seconds


def validate_intervals(lease_seconds, heartbeat_seconds):
    lease_seconds = valid_lease_seconds(lease_seconds)
    if (
        type(heartbeat_seconds) not in (int, float)
        or not math.isfinite(heartbeat_seconds)
        or not 0.05 <= heartbeat_seconds <= lease_seconds / 3
    ):
        raise ValueError(
            "Heartbeat interval must be at least 0.05 seconds and at most one third of the lease"
        )


class LeaseHeartbeat:
    def __init__(self, store, claim, interval=5):
        validate_intervals(claim.lease_seconds, interval)
        self.store, self.claim, self.interval = store, claim, interval
        self.stopped, self.failed = Event(), Event()
        self.failure_code = None
        self.engine = engine_for(
            store.database,
            statement_timeout_ms=1000,
            connect_timeout_seconds=2,
            sqlite_timeout_seconds=0.25,
        )
        self.thread = Thread(
            target=self._loop, name="safeharness-heartbeat", daemon=True
        )

    def _beat(self):
        try:
            if not renew_claim(self.store, self.claim, engine=self.engine):
                self.stopped.set()
                return False
            return True
        except Exception:  # noqa: BLE001 - never let an unobserved thread error preserve authority
            self.failure_code = "heartbeat_failed"
            self.claim.fenced.set()
            self.failed.set()
            self.stopped.set()
            return False

    def _loop(self):
        while not self.stopped.wait(self.interval):
            if not self._beat():
                return

    def __enter__(self):
        if self._beat():
            self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.failure_code = "heartbeat_shutdown_timeout"
                self.claim.fenced.set()
                self.failed.set()
        self.engine.dispose()
