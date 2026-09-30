"""Small persistent state store for values acknowledged by the charger."""

from __future__ import annotations

import json
import logging
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


class StateStore:
    """Persist the last charger-acknowledged values as JSON in /data."""

    def __init__(self, directory: Path, default_current: float) -> None:
        self._directory = directory
        self._path = directory / "state.json"
        self.state = PersistentState(maximum_current=default_current)

    def load(self) -> PersistentState:
        """Load state, falling back safely when the file is absent or invalid."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self.state = PersistentState(
                maximum_current=float(payload["maximum_current"]),
                last_transaction_id=int(payload.get("last_transaction_id", 0)),
            )
        except FileNotFoundError:
            LOGGER.info("No persisted state found; using configured defaults")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
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
                json.dump(asdict(self.state), file_handle, sort_keys=True)
                file_handle.write("\n")
            os.replace(temporary_name, self._path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
