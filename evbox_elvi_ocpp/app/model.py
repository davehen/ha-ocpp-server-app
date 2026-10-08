"""One connection-scoped model. Observations are never command acknowledgments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

ACTIVE_STATUSES = {"Charging", "SuspendedEV", "SuspendedEVSE"}
IDLE_STATUSES = {"Available", "Preparing", "Finishing"}
VALID_STATUSES = ACTIVE_STATUSES | IDLE_STATUSES | {"Faulted", "Unavailable", "Reserved"}


@dataclass
class ConnectorState:
    """Live facts, discarded together on reconnect or BootNotification.

    The persisted recovery target lives in StateStore, not here. A scope is
    invalidated by a connection/boot reset or a transaction boundary, but not
    by a Charging/Suspended transition within the same transaction.
    """

    epoch: int = 0
    session: int = 0
    status: str | None = None
    active: bool | None = None
    transaction_id: int | None = None
    power_kw: float | None = None
    current_a: float | None = None
    active_limit: float | None = None
    default_limit: float | None = None
    last_status_time: datetime | None = None
    last_meter_time: datetime | None = None

    @property
    def scope(self) -> tuple[int, int, int | None]:
        return self.epoch, self.session, self.transaction_id

    @property
    def availability(self) -> bool | None:
        """Free/occupied compatibility signal, not an inferred cable sensor."""
        if self.status in {"Faulted", "Unavailable", "Reserved"}:
            return None
        if self.transaction_id is not None or self.active:
            return False
        if self.status in IDLE_STATUSES:
            return self.status == "Available"
        return None

    @property
    def limit(self) -> float | None:
        if self.transaction_id is not None:
            return self.active_limit
        if self.active is False:
            return self.default_limit
        return None

    @property
    def can_restore_limit(self) -> bool:
        return self.transaction_id is not None or self.active is False

    def reset(self) -> None:
        fresh = ConnectorState(epoch=self.epoch + 1)
        self.__dict__.update(fresh.__dict__)

    def open_transaction(self, transaction_id: int) -> None:
        if transaction_id != self.transaction_id:
            self.session += 1
            self.transaction_id = transaction_id
            self.active_limit = None
            self.power_kw = self.current_a = None
            self.last_meter_time = None
        self.active = True

    def close_transaction(self) -> int | None:
        previous = self.transaction_id
        if previous is not None or self.active is not False:
            self.session += 1
        self.transaction_id = None
        self.active_limit = None
        self.active = False
        self.power_kw = self.current_a = 0
        return previous

    def observe_status(self, status: str, timestamp: datetime | None) -> bool:
        if status not in VALID_STATUSES:
            return False
        if timestamp and self.last_status_time and timestamp < self.last_status_time:
            return False
        if timestamp:
            self.last_status_time = timestamp
        previous = self.status
        self.status = status
        if status in {"Available", "Finishing"}:
            self.close_transaction()
        elif status in ACTIVE_STATUSES:
            self.active = True
            if status != "Charging":
                self.power_kw = self.current_a = 0
            elif previous != "Charging":
                self.power_kw = self.current_a = None
        elif status == "Preparing" and self.transaction_id is None:
            self.active = False
            self.power_kw = self.current_a = 0
        # Fault/Unavailable/Reserved are not StopTransaction, nor cable evidence.
        # Keep an identified session, otherwise leave session state unknown.
        elif status in {"Faulted", "Unavailable", "Reserved"}:
            self.active = True if self.transaction_id is not None else None
            self.power_kw = self.current_a = None
        return True

    def observe_meter(
        self, timestamp: datetime | None, power: float | None, current: float | None
    ) -> bool:
        if timestamp and self.last_meter_time and timestamp < self.last_meter_time:
            return False
        # Cross-action ordering matters: buffered meters must not undo a later
        # suspension/stop status, even when they are the newest meter packet.
        if timestamp and self.last_status_time and timestamp < self.last_status_time:
            return False
        if timestamp:
            self.last_meter_time = timestamp
        if power is not None:
            self.power_kw = power
        if current is not None:
            self.current_a = current
        return True
