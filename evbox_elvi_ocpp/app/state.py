"""Small persistent state store for values acknowledged by the charger."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass
class PersistentState:
    """State that must survive app restarts."""

    maximum_current: float
    last_transaction_id: int = 0
    last_closed_transaction_id: int = -1


class StateStore:
    """Persist the last charger-acknowledged values as JSON in /data."""

    def __init__(self, directory: Path, default_current: float) -> None:
        self._directory = directory
        self._path = directory / "state.json"
        self.state = PersistentState(maximum_current=default_current)
        self._maximum_current = default_current
        self.valid = True

    def load(self) -> PersistentState:
        """Load state, falling back safely when the file is absent or invalid."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(payload["maximum_current"], bool):
                raise ValueError("Saved current must be numeric, not boolean")
            current = float(payload["maximum_current"])
            last_id = payload.get("last_transaction_id", 0)
            closed_id = payload.get("last_closed_transaction_id", -1)
            if type(last_id) is not int or type(closed_id) is not int:
                raise ValueError("Saved transaction IDs must be integers")
            if not math.isfinite(current) or not 0 <= current <= self._maximum_current:
                raise ValueError("Saved current is outside the configured range")
            if not 0 <= last_id <= 2**31 - 1 or not -1 <= closed_id <= last_id:
                raise ValueError("Invalid saved transaction counter")
            self.state = PersistentState(
                maximum_current=current,
                last_transaction_id=last_id,
                last_closed_transaction_id=closed_id,
            )
            self.valid = True
        except FileNotFoundError:
            LOGGER.info("No persisted state found; using configured defaults")
        except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError, OSError):
            self.valid = False
            LOGGER.exception("Ignoring invalid persisted state at %s", self._path)
        return self.state

    def save(self) -> None:
        """Atomically save state."""
        self._directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix="state-",
            suffix=".json",
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
                json.dump(asdict(self.state), file_handle, sort_keys=True, allow_nan=False)
                file_handle.write("\n")
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary_name, self._path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
