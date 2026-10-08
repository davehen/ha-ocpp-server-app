"""OCPP event reduction and explicit MQTT commands; no charging policy."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .model import ConnectorState
from .mqtt import InvalidMqttCommand, MqttBridge, MqttCommandRejected
from .ocpp import OcppCallError, OcppConnection, OcppNotSupportedError
from .state import StateStore

LOGGER = logging.getLogger(__name__)
SUPPORTED_METER_VALUES = (
    "Energy.Active.Import.Register,Power.Active.Import,Current.Import,"
    "Current.Offered,Voltage,Frequency,Temperature"
)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class BridgeController:
    """One state owner; serialized effects cannot commit into retired scopes.

    Receiving an event never awaits an outgoing command. Readiness is derived
    from observations, not fabricated by changing the observed session state.
    """

    def __init__(self, config: Config, mqtt_bridge: MqttBridge, state_store: StateStore) -> None:
        self._config, self._mqtt, self._store = config, mqtt_bridge, state_store
        self._store.load()
        self.state = ConnectorState()
        self._connection: OcppConnection | None = None
        self._command_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._initialized_epoch: int | None = None
        self._restore_attempt: tuple[int, int, int | None] | None = None
        self._maintenance_dirty = False
        mqtt_bridge.set_command_handler(self.handle_mqtt_command)

    def _publish(self) -> None:
        """One derived snapshot, measurements/limit before switches."""
        self._mqtt.publish_power(self.state.power_kw)
        self._mqtt.publish_current(self.state.current_a)
        self._mqtt.publish_maximum_current(self.state.limit)
        self._mqtt.publish_charger_availability(self.state.availability)
        self._mqtt.publish_charge_control(self.state.active)

    def _reset(self) -> None:
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
        self._maintenance_task = None
        self.state.reset()
        self._initialized_epoch = self._restore_attempt = None
        self._maintenance_dirty = False
        self._publish()

    def start(self) -> None:
        self._mqtt.publish_charger_online(False)
        self._reset()

    async def attach(self, connection: OcppConnection) -> None:
        previous = self._connection
        self._mqtt.publish_charger_online(False)
        self._connection = connection
        self._reset()
        self._mqtt.publish_charger_online(True)
        if previous is not None and previous is not connection and not previous.closed:
            await previous.close(code=1012, reason="Reconnected")

    def detach(self, connection: OcppConnection) -> None:
        if connection is self._connection:
            self._mqtt.publish_charger_online(False)
            self._connection = None
            self._reset()

    def _require_connection(self) -> OcppConnection:
        if self._connection is None or self._connection.closed:
            raise ConnectionError("EVBox Elvi is not connected through OCPP")
        return self._connection

    def _check_scope(self, connection: OcppConnection, scope: tuple[int, int, int | None]) -> None:
        if connection is not self._connection or connection.closed or scope != self.state.scope:
            raise MqttCommandRejected("Discarding command result for a retired connection/session")

    async def handle_ocpp_call(
        self, action: str, payload: dict[str, Any], *, connection: OcppConnection | None = None
    ) -> dict[str, Any]:
        if connection is not None and connection is not self._connection:
            raise ConnectionError("Message belongs to a retired connection")
        if action == "BootNotification":
            self._reset()
            self._mqtt.update_device_information(
                manufacturer=_string(payload.get("chargePointVendor")),
                model=_string(payload.get("chargePointModel")),
                serial_number=_string(
                    payload.get("chargePointSerialNumber") or payload.get("chargeBoxSerialNumber")
                ),
                firmware_version=_string(payload.get("firmwareVersion")),
            )
            return {
                "currentTime": utc_timestamp(),
                "interval": self._config.heartbeat_interval,
                "status": "Accepted",
            }
        if action == "Heartbeat":
            return {"currentTime": utc_timestamp()}
        if action == "Authorize":
            return {"idTagInfo": {"status": "Accepted"}}
        if action == "DataTransfer":
            return {"status": "Accepted"}
        if action in {"DiagnosticsStatusNotification", "FirmwareStatusNotification"}:
            return {}
        if action == "StatusNotification":
            if _integer(payload.get("connectorId")) == 1:
                previous_id = self.state.transaction_id
                if self.state.observe_status(
                    str(payload.get("status", "")), _timestamp(payload.get("timestamp"))
                ):
                    if previous_id is not None and self.state.transaction_id is None:
                        self._record_closed(previous_id)
                    self._publish()
            return {}
        if action == "StartTransaction":
            if _integer(payload.get("connectorId")) != 1:
                raise ValueError("Only connector 1 is supported")
            if self.state.transaction_id is not None:
                raise ValueError("Conflicting StartTransaction on an identified active session")
            transaction_id = max(int(time.time()), self._store.state.last_transaction_id + 1)
            if transaction_id > 2**31 - 1:
                raise ValueError("Transaction counter exhausted")
            self._store.state.last_transaction_id = transaction_id
            self._store.save()  # Never acknowledge an allocation that cannot survive restart.
            self.state.open_transaction(transaction_id)
            self.state.status = None
            self._publish()
            return {"transactionId": transaction_id, "idTagInfo": {"status": "Accepted"}}
        if action == "StopTransaction":
            transaction_id = _integer(payload.get("transactionId"))
            if transaction_id is not None:
                if transaction_id == self.state.transaction_id or (
                    self.state.transaction_id is None and self.state.active is None
                ):
                    self._record_closed(transaction_id)
                    self.state.close_transaction()
                    self.state.status = None
                    timestamp = _timestamp(payload.get("timestamp"))
                    if timestamp and (
                        self.state.last_status_time is None
                        or timestamp > self.state.last_status_time
                    ):
                        self.state.last_status_time = timestamp
                    self._publish()
                elif self.state.transaction_id is None:
                    self._record_closed(transaction_id)
            return {"idTagInfo": {"status": "Accepted"}}
        if action == "MeterValues":
            self._observe_meter(payload)
            return {}
        raise OcppNotSupportedError(f"Action {action} is not implemented")

    def _observe_meter(self, payload: dict[str, Any]) -> None:
        if _integer(payload.get("connectorId")) != 1:
            return
        timestamp = _latest_meter_time(payload)
        if timestamp and (
            (self.state.last_meter_time and timestamp < self.state.last_meter_time)
            or (self.state.last_status_time and timestamp < self.state.last_status_time)
        ):
            return
        transaction_id = _integer(payload.get("transactionId"))
        if "transactionId" in payload:
            if (
                transaction_id is None
                or transaction_id <= self._store.state.last_closed_transaction_id
                or (
                    self.state.transaction_id is not None
                    and transaction_id != self.state.transaction_id
                )
                or (self.state.active is False and self.state.status in {"Available", "Finishing"})
            ):
                return
            if self.state.transaction_id is None:
                self.state.open_transaction(transaction_id)
                self._store.state.last_transaction_id = max(
                    self._store.state.last_transaction_id, transaction_id
                )
                self._save_state_safely()
        self.state.observe_meter(
            timestamp,
            extract_power_kw(payload, self._config.number_of_phases),
            extract_current_a(payload),
        )
        self._publish()

    def _record_closed(self, transaction_id: int) -> None:
        self._store.state.last_transaction_id = max(
            self._store.state.last_transaction_id, transaction_id
        )
        self._store.state.last_closed_transaction_id = max(
            self._store.state.last_closed_transaction_id, transaction_id
        )
        self._save_state_safely()

    def _save_state_safely(self) -> None:
        try:
            self._store.save()
        except OSError:
            LOGGER.exception("Cannot persist state; live observations remain valid")

    async def after_ocpp_call(
        self, action: str, *, connection: OcppConnection | None = None
    ) -> None:
        """Bounded recovery, scheduled AFTER the incoming CALLRESULT was sent."""
        if connection is not None and connection is not self._connection:
            return
        if self._connection is None or self._connection.closed:
            return
        self._maintenance_dirty = True
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(self._maintain())

    async def _maintain(self) -> None:
        while self._maintenance_dirty:
            self._maintenance_dirty = False
            await self._maintenance_pass()

    async def _maintenance_pass(self) -> None:
        try:
            await self._restore_limit()
            if self._initialized_epoch != self.state.epoch:
                self._initialized_epoch = self.state.epoch
                await self._optional_call(
                    "TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 1}
                )
                if self.state.active and self.state.transaction_id is None:
                    await self._optional_call(
                        "TriggerMessage", {"requestedMessage": "MeterValues", "connectorId": 1}
                    )
                if self._config.configure_meter_values:
                    await self._ensure_configuration()
            await self._restore_limit()  # Events may have arrived during optional calls.
        except asyncio.CancelledError:
            raise
        except (MqttCommandRejected, ConnectionError, TimeoutError, OcppCallError) as error:
            LOGGER.warning("Recovery incomplete (no automatic start/stop): %s", error)
        except Exception:
            LOGGER.exception("Unexpected recovery failure; OCPP listener remains active")

    async def _restore_limit(self) -> None:
        async with self._command_lock:
            if (
                not self.state.can_restore_limit
                or self.state.limit == self._store.state.maximum_current
                or self._restore_attempt == self.state.scope
                or not self._store.valid
            ):
                return
            self._restore_attempt = self.state.scope
            await self._set_current(self._store.state.maximum_current)

    async def _optional_call(self, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Each optional operation releases the command lock; never a readiness gate."""
        async with self._command_lock:
            connection, epoch = self._require_connection(), self.state.epoch
            try:
                response = await connection.call(action, payload)
                # Configuration/status requests are connection-scoped, not
                # transaction-scoped. Their requested notification may itself
                # change the session while the response is in flight.
                if connection is not self._connection or epoch != self.state.epoch:
                    raise MqttCommandRejected(
                        "Optional result belongs to a retired connection/reboot"
                    )
                return response
            except (OcppCallError, MqttCommandRejected, ConnectionError, TimeoutError) as error:
                LOGGER.warning("Optional %s unavailable: %s", action, error)
                return None

    async def _ensure_configuration(self) -> None:
        desired = {
            "MeterValuesSampledData": SUPPORTED_METER_VALUES,
            "MeterValueSampleInterval": str(self._config.meter_value_interval),
        }
        response = await self._optional_call("GetConfiguration", {"key": list(desired)})
        if response is None:
            return
        for item in response.get("configurationKey", []):
            key = item.get("key")
            if key in desired and not item.get("readonly") and item.get("value") != desired[key]:
                await self._optional_call(
                    "ChangeConfiguration", {"key": key, "value": desired[key]}
                )

    async def handle_mqtt_command(self, topic: str, payload: str) -> None:
        connection, scope = self._require_connection(), self.state.scope
        async with self._command_lock:
            self._check_scope(connection, scope)
            if topic == self._mqtt.topic(f"{MqttBridge.MAXIMUM_CURRENT}/set"):
                try:
                    amperes = float(payload)
                except ValueError:
                    raise InvalidMqttCommand(
                        f"Invalid maximum-current payload: {payload!r}"
                    ) from None
                await self._set_current(amperes)
            elif topic == self._mqtt.topic(f"{MqttBridge.CHARGE_CONTROL}/set"):
                if payload == "ON":
                    await self._start_charging()
                elif payload == "OFF":
                    transaction_id = self.state.transaction_id
                    if transaction_id is None:
                        raise MqttCommandRejected("Cannot stop without a recovered transaction ID")
                    await self._accepted_call(
                        "RemoteStopTransaction", {"transactionId": transaction_id}
                    )
                else:
                    raise InvalidMqttCommand(f"Invalid charge-control payload: {payload!r}")
            elif topic == self._mqtt.topic(f"{MqttBridge.CHARGER_AVAILABILITY}/set"):
                if payload not in {"ON", "OFF"}:
                    raise InvalidMqttCommand(f"Invalid availability payload: {payload!r}")
                await self._accepted_call(
                    "ChangeAvailability",
                    {"connectorId": 1, "type": "Operative" if payload == "ON" else "Inoperative"},
                    accepted={"Accepted", "Scheduled"},
                )
            else:
                raise InvalidMqttCommand(f"Unknown MQTT command topic: {topic}")

    async def _accepted_call(
        self, action: str, payload: dict[str, Any], *, accepted: set[str] | None = None
    ) -> dict[str, Any]:
        response = await self._require_connection().call(action, payload)
        if response.get("status") not in (accepted or {"Accepted"}):
            raise MqttCommandRejected(f"EVBox rejected {action}: {response}")
        return response

    async def _start_charging(self) -> None:
        if not self._store.valid:
            raise MqttCommandRejected("Set a valid current explicitly before remote start")
        # Firmware, not a local status whitelist, decides whether start is valid.
        amperes = self._store.state.maximum_current
        connection, epoch = self._require_connection(), self.state.epoch
        await self._set_default(amperes)
        await self._accepted_call(
            "RemoteStartTransaction",
            {
                "connectorId": 1,
                "idTag": self._config.id_tag,
                "chargingProfile": self._current_profile(amperes, active=True),
            },
        )
        if connection is not self._connection or epoch != self.state.epoch:
            raise MqttCommandRejected(
                "Remote start response belongs to a retired connection/reboot"
            )
        LOGGER.info(
            "EVBox accepted remote start with %.1f A profile; awaiting session event", amperes
        )

    def _validate_current(self, amperes: float) -> None:
        if not math.isfinite(amperes) or not 0 <= amperes <= self._config.maximum_current:
            raise InvalidMqttCommand(
                f"Current must be finite and between 0 and {self._config.maximum_current:g} A"
            )

    async def _set_default(self, amperes: float) -> None:
        connection, scope = self._require_connection(), self.state.scope
        try:
            await self._accepted_call(
                "SetChargingProfile",
                {
                    "connectorId": 1,
                    "csChargingProfiles": self._current_profile(amperes, active=False),
                },
            )
        except (TimeoutError, ConnectionError):
            if connection is self._connection and scope == self.state.scope:
                self.state.default_limit = None
                self._publish()
            raise
        self._check_scope(connection, scope)
        self.state.default_limit = amperes
        self._publish()
        LOGGER.info("EVBox accepted %.1f A TxDefaultProfile", amperes)

    async def _set_current(self, amperes: float) -> None:
        self._validate_current(amperes)
        connection, scope = self._require_connection(), self.state.scope
        transaction_id = self.state.transaction_id
        if self.state.active and transaction_id is None:
            raise MqttCommandRejected("Active session observed; waiting for its transaction ID")
        # Explicit repeated values are forwarded, useful after firmware ignored an ACK.
        if transaction_id is not None:
            try:
                response = await connection.call(
                    "SetChargingProfile",
                    {
                        "connectorId": 1,
                        "csChargingProfiles": self._current_profile(
                            amperes, active=True, transaction_id=transaction_id
                        ),
                    },
                )
            except (TimeoutError, ConnectionError):
                if connection is self._connection and scope == self.state.scope:
                    self.state.active_limit = None
                    self._publish()
                raise
            self._check_scope(connection, scope)
            if response.get("status") != "Accepted":
                raise MqttCommandRejected(
                    f"EVBox rejected SetChargingProfile(TxProfile): {response}"
                )
            self.state.active_limit = amperes
            self._commit_target(amperes)
            LOGGER.info(
                "EVBox accepted %.1f A TxProfile for transaction %s", amperes, transaction_id
            )
            try:
                await self._set_default(amperes)
            except (MqttCommandRejected, OcppCallError, TimeoutError, ConnectionError) as error:
                LOGGER.warning(
                    "Active profile accepted; next-session default unconfirmed: %s", error
                )
        else:
            await self._set_default(amperes)
            self._commit_target(amperes)

    def _commit_target(self, amperes: float) -> None:
        self._store.state.maximum_current = amperes
        self._store.valid = True
        self._save_state_safely()
        self._publish()

    @staticmethod
    def _current_profile(
        amperes: float, *, active: bool, transaction_id: int | None = None
    ) -> dict[str, Any]:
        profile = {
            "chargingProfileId": 2002 if active else 2001,
            "stackLevel": 1 if active else 0,
            "chargingProfilePurpose": "TxProfile" if active else "TxDefaultProfile",
            "chargingProfileKind": "Relative",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": amperes}],
            },
        }
        if transaction_id is not None:
            profile["transactionId"] = transaction_id
        return profile


def extract_power_kw(payload: dict[str, Any], number_of_phases: int) -> float | None:
    samples = _latest_samples(payload)
    powers = {}
    for sample in samples:
        if sample.get("measurand") == "Power.Active.Import":
            watts = _to_watts(sample)
            phase = str(sample.get("phase", "")).removesuffix("-N")
            if watts is not None and phase in {"", "L1", "L2", "L3"}:
                powers[phase] = watts
    if "" in powers:
        return powers[""] / 1000
    if powers:
        return sum(powers.values()) / 1000
    currents = _values_by_phase([s for s in samples if s.get("unit", "A") == "A"], "Current.Import")
    voltages = _values_by_phase([s for s in samples if s.get("unit", "V") == "V"], "Voltage")
    if not currents:
        return None
    phased_currents = {p: v for p, v in currents.items() if p in {"L1", "L2", "L3"}}
    phased_voltages = {p: v for p, v in voltages.items() if p}
    matched = [
        value * voltage
        for phase, value in phased_currents.items()
        if (voltage := phased_voltages.get(phase, phased_voltages.get(f"{phase}-N"))) is not None
    ]
    if matched:
        return sum(matched) / 1000
    current = currents.get("")
    if current is None:
        current = sum(phased_currents.values()) / max(len(phased_currents), 1)
    voltage = voltages.get("")
    if voltage is None:
        voltage = sum(phased_voltages.values()) / len(phased_voltages) if phased_voltages else 230
    return current * voltage * number_of_phases / 1000


def extract_current_a(payload: dict[str, Any]) -> float | None:
    currents = _values_by_phase(
        [s for s in _latest_samples(payload) if s.get("unit", "A") == "A"], "Current.Import"
    )
    if "" in currents:
        return currents[""]
    phases = [v for p, v in currents.items() if p in {"L1", "L2", "L3"}]
    active = [v for v in phases if v > 0]
    return sum(active) / len(active) if active else 0.0 if phases else None


def _latest_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    meters = [m for m in payload.get("meterValue", []) if isinstance(m, dict)]
    return [s for s in meters[-1].get("sampledValue", []) if isinstance(s, dict)] if meters else []


def _values_by_phase(samples: list[dict[str, Any]], measurand: str) -> dict[str, float]:
    result = {}
    for sample in samples:
        if sample.get("measurand") != measurand:
            continue
        try:
            value = float(sample["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            result[str(sample.get("phase", ""))] = value
    return result


def _to_watts(sample: dict[str, Any]) -> float | None:
    try:
        value = float(sample["value"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = sample.get("unit", "W")
    return (
        value * (1000 if unit == "kW" else 1)
        if math.isfinite(value) and value >= 0 and unit in {"W", "kW"}
        else None
    )


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(UTC) if timestamp.tzinfo else None
    except ValueError:
        return None


def _latest_meter_time(payload: dict[str, Any]) -> datetime | None:
    values = payload.get("meterValue", [])
    return (
        _timestamp(values[-1].get("timestamp")) if values and isinstance(values[-1], dict) else None
    )


def _string(value: Any) -> str | None:
    return (str(value).strip() or None) if value is not None else None
