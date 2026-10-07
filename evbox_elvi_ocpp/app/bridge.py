"""OCPP protocol behavior and the small MQTT-facing command surface."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .mqtt import InvalidMqttCommand, MqttBridge, MqttCommandRejected
from .ocpp import OcppConnection, OcppNotSupportedError
from .state import StateStore

LOGGER = logging.getLogger(__name__)

ACCEPTED = "Accepted"
CHARGING_STATUSES = {"Charging", "SuspendedEV", "SuspendedEVSE"}
ZERO_POWER_STATUSES = {
    "Available",
    "Faulted",
    "Finishing",
    "Preparing",
    "Reserved",
    "SuspendedEV",
    "SuspendedEVSE",
    "Unavailable",
}
SUPPORTED_METER_VALUES = (
    "Energy.Active.Import.Register,Power.Active.Import,Current.Import,"
    "Current.Offered,Voltage,Frequency,Temperature"
)


def utc_timestamp() -> str:
    """Return an OCPP-compatible UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class BridgeController:
    """Translate protocol state and explicit MQTT commands without automation logic."""

    def __init__(self, config: Config, mqtt_bridge: MqttBridge, state_store: StateStore) -> None:
        self._config = config
        self._mqtt = mqtt_bridge
        self._store = state_store
        self._connection: OcppConnection | None = None
        self._command_lock = asyncio.Lock()
        self._initialization_task: asyncio.Task[None] | None = None
        self._status = "Unavailable"
        self._charge_control = False
        self._transaction_id: int | None = None
        self._store.load()
        mqtt_bridge.set_command_handler(self.handle_mqtt_command)

    def start(self) -> None:
        """Publish retained initial states after MQTT is connected."""
        self._mqtt.publish_charger_online(False)
        self._mqtt.publish_charge_control(False)
        self._mqtt.publish_charger_availability(False)
        self._mqtt.publish_power(0)
        self._mqtt.publish_current(0)
        self._mqtt.publish_maximum_current(self._store.state.maximum_current)

    async def attach(self, connection: OcppConnection) -> None:
        """Attach the newest charge point connection, replacing a stale one."""
        previous = self._connection
        self._connection = connection
        self._status = "Unavailable"
        self._charge_control = False
        self._transaction_id = None
        self._mqtt.publish_charger_online(False)
        if previous is not None and previous is not connection and not previous.closed:
            LOGGER.warning("Replacing an existing OCPP connection")
            await previous.close(code=1012, reason="Reconnected")

    def detach(self, connection: OcppConnection) -> None:
        """Mark entities unavailable if the active charge point disconnected."""
        if self._connection is not connection:
            return
        self._connection = None
        if self._initialization_task is not None:
            self._initialization_task.cancel()
            self._initialization_task = None
        self._mqtt.publish_charger_online(False)

    async def handle_ocpp_call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the OCPP 1.6 response for one charge-point initiated call."""
        handlers = {
            "Authorize": self._on_authorize,
            "BootNotification": self._on_boot_notification,
            "DataTransfer": self._on_data_transfer,
            "DiagnosticsStatusNotification": self._on_empty_notification,
            "FirmwareStatusNotification": self._on_empty_notification,
            "Heartbeat": self._on_heartbeat,
            "MeterValues": self._on_meter_values,
            "StartTransaction": self._on_start_transaction,
            "StatusNotification": self._on_status_notification,
            "StopTransaction": self._on_stop_transaction,
        }
        handler = handlers.get(action)
        if handler is None:
            raise OcppNotSupportedError(f"Action {action} is not implemented by this frozen bridge")
        return handler(payload)

    async def after_ocpp_call(self, action: str) -> None:
        """Run post-response work without blocking the WebSocket receive loop."""
        if action != "BootNotification":
            return
        if self._initialization_task is not None:
            self._initialization_task.cancel()
        self._initialization_task = asyncio.create_task(
            self._initialize_charger_safely(),
            name="initialize-evbox-elvi",
        )

    async def handle_mqtt_command(self, topic: str, payload: str) -> None:
        """Execute one command requested by a Home Assistant MQTT entity."""
        async with self._command_lock:
            if topic == self._mqtt.topic(f"{MqttBridge.CHARGE_CONTROL}/set"):
                if payload.upper() == "ON":
                    await self._start_charging()
                elif payload.upper() == "OFF":
                    await self._stop_charging()
                else:
                    raise InvalidMqttCommand(
                        f"Invalid charge-control payload: {payload!r}"
                    )
                return
            if topic == self._mqtt.topic(f"{MqttBridge.CHARGER_AVAILABILITY}/set"):
                if payload.upper() not in {"ON", "OFF"}:
                    raise InvalidMqttCommand(f"Invalid availability payload: {payload!r}")
                await self._set_availability(payload.upper() == "ON")
                return
            if topic == self._mqtt.topic(f"{MqttBridge.MAXIMUM_CURRENT}/set"):
                try:
                    amperes = float(payload)
                except ValueError:
                    raise InvalidMqttCommand(
                        f"Invalid maximum-current payload: {payload!r}"
                    ) from None
                await self._set_current(amperes)
                return
            raise InvalidMqttCommand(f"Unknown MQTT command topic: {topic}")

    def _on_boot_notification(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mqtt.update_device_information(
            manufacturer=_string(payload.get("chargePointVendor")),
            model=_string(payload.get("chargePointModel")),
            serial_number=_string(
                payload.get("chargePointSerialNumber") or payload.get("chargeBoxSerialNumber")
            ),
            firmware_version=_string(payload.get("firmwareVersion")),
        )
        self._mqtt.publish_charge_control(False)
        self._mqtt.publish_charger_availability(False)
        self._mqtt.publish_power(0)
        self._mqtt.publish_current(0)
        self._mqtt.publish_charger_online(True)
        return {
            "currentTime": utc_timestamp(),
            "interval": self._config.heartbeat_interval,
            "status": ACCEPTED,
        }

    @staticmethod
    def _on_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
        return {"currentTime": utc_timestamp()}

    @staticmethod
    def _on_authorize(payload: dict[str, Any]) -> dict[str, Any]:
        return {"idTagInfo": {"status": ACCEPTED}}

    def _on_status_notification(self, payload: dict[str, Any]) -> dict[str, Any]:
        connector_id = _integer(payload.get("connectorId"), default=0)
        if connector_id not in {0, 1}:
            return {}
        status = _string(payload.get("status"))
        if not status:
            return {}
        self._status = status
        if status in CHARGING_STATUSES:
            self._charge_control = True
        elif status in {"Available", "Unavailable", "Faulted"}:
            self._charge_control = False
            if status == "Available":
                self._transaction_id = None
        self._mqtt.publish_charge_control(self._charge_control)
        self._mqtt.publish_charger_availability(status == "Available")
        if status in ZERO_POWER_STATUSES:
            self._mqtt.publish_power(0)
            self._mqtt.publish_current(0)
        return {}

    def _on_start_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        transaction_id = max(now, self._store.state.last_transaction_id + 1)
        self._store.state.last_transaction_id = transaction_id
        self._store.save()
        self._transaction_id = transaction_id
        self._charge_control = True
        self._mqtt.publish_charge_control(True)
        return {"transactionId": transaction_id, "idTagInfo": {"status": ACCEPTED}}

    def _on_stop_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._transaction_id = None
        self._charge_control = False
        self._mqtt.publish_charge_control(False)
        self._mqtt.publish_power(0)
        self._mqtt.publish_current(0)
        return {"idTagInfo": {"status": ACCEPTED}}

    def _on_meter_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = payload.get("transactionId")
        if transaction_id is not None:
            self._transaction_id = _integer(transaction_id, default=self._transaction_id)
        power_kw = extract_power_kw(payload, self._config.number_of_phases)
        if power_kw is not None:
            self._mqtt.publish_power(power_kw)
        current_a = extract_current_a(payload)
        if current_a is not None:
            self._mqtt.publish_current(current_a)
        return {}

    @staticmethod
    def _on_data_transfer(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": ACCEPTED}

    @staticmethod
    def _on_empty_notification(payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _initialize_charger_safely(self) -> None:
        try:
            if self._config.configure_meter_values:
                await self._ensure_configuration(
                    {
                        "MeterValuesSampledData": SUPPORTED_METER_VALUES,
                        "MeterValueSampleInterval": str(self._config.meter_value_interval),
                    }
                )
            connection = self._require_connection()
            response = await connection.call(
                "TriggerMessage",
                {"requestedMessage": "StatusNotification", "connectorId": 1},
            )
            if not _is_accepted(response):
                LOGGER.warning("EVBox rejected TriggerMessage(StatusNotification): %s", response)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Best-effort EVBox initialization failed; OCPP service remains active")

    async def _ensure_configuration(self, desired: dict[str, str]) -> None:
        response = await self._require_connection().call(
            "GetConfiguration",
            {"key": list(desired)},
        )
        configuration = response.get("configurationKey", [])
        if not isinstance(configuration, list):
            LOGGER.warning("EVBox returned an invalid GetConfiguration response: %s", response)
            return
        current_by_key = {
            item.get("key"): item
            for item in configuration
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        for key, desired_value in desired.items():
            current = current_by_key.get(key)
            if current is None:
                LOGGER.warning("EVBox did not report configuration key %s", key)
                continue
            if str(current.get("value", "")) == desired_value:
                continue
            if current.get("readonly") is True:
                LOGGER.warning("EVBox configuration key %s is read-only", key)
                continue
            await self._change_configuration(key, desired_value)

    async def _change_configuration(self, key: str, value: str) -> None:
        response = await self._require_connection().call(
            "ChangeConfiguration",
            {"key": key, "value": value},
        )
        if not _is_accepted(response):
            LOGGER.warning("EVBox rejected ChangeConfiguration(%s): %s", key, response)

    async def _start_charging(self) -> None:
        response = await self._require_connection().call(
            "RemoteStartTransaction",
            {"idTag": self._config.id_tag, "connectorId": 1},
        )
        if not _is_accepted(response):
            raise MqttCommandRejected(
                f"EVBox rejected RemoteStartTransaction: {response}"
            )
        self._charge_control = True
        self._mqtt.publish_charge_control(True)

    async def _stop_charging(self) -> None:
        if self._transaction_id is None:
            raise MqttCommandRejected(
                "Cannot stop charging before an OCPP transaction ID is known"
            )
        response = await self._require_connection().call(
            "RemoteStopTransaction",
            {"transactionId": self._transaction_id},
        )
        if not _is_accepted(response):
            raise MqttCommandRejected(
                f"EVBox rejected RemoteStopTransaction: {response}"
            )
        self._charge_control = False
        self._mqtt.publish_charge_control(False)

    async def _set_availability(self, available: bool) -> None:
        response = await self._require_connection().call(
            "ChangeAvailability",
            {"connectorId": 1, "type": "Operative" if available else "Inoperative"},
        )
        if not _is_accepted(response, accepted_values={"Accepted", "Scheduled"}):
            raise MqttCommandRejected(f"EVBox rejected ChangeAvailability: {response}")

    async def _set_current(self, amperes: float) -> None:
        if not math.isfinite(amperes) or amperes < 0 or amperes > self._config.maximum_current:
            raise InvalidMqttCommand(
                f"Current must be between 0 and {self._config.maximum_current:g} A"
            )
        transaction_id = self._transaction_id
        if transaction_id is not None:
            transaction_profile = self._current_profile(
                amperes,
                profile_id=2002,
                purpose="TxProfile",
                stack_level=1,
                transaction_id=transaction_id,
            )
            response = await self._require_connection().call(
                "SetChargingProfile",
                {"connectorId": 1, "csChargingProfiles": transaction_profile},
            )
            if not _is_accepted(response):
                raise MqttCommandRejected(
                    f"EVBox rejected SetChargingProfile(TxProfile): {response}"
                )
            LOGGER.info(
                "EVBox accepted %.1f A TxProfile for transaction %s",
                amperes,
                transaction_id,
            )

            # Keep the same limit as the default for the next transaction. The
            # active TxProfile above is the critical command, so a charger that
            # rejects this best-effort follow-up must not turn an already
            # applied dynamic limit into a reported command failure.
            try:
                default_response = await self._require_connection().call(
                    "SetChargingProfile",
                    {
                        "connectorId": 1,
                        "csChargingProfiles": self._current_profile(
                            amperes,
                            profile_id=2001,
                            purpose="TxDefaultProfile",
                            stack_level=0,
                        ),
                    },
                )
                if not _is_accepted(default_response):
                    LOGGER.warning(
                        "EVBox accepted the active TxProfile but rejected the "
                        "TxDefaultProfile for the next transaction: %s",
                        default_response,
                    )
                else:
                    LOGGER.info(
                        "EVBox accepted %.1f A TxDefaultProfile for the next transaction",
                        amperes,
                    )
            except Exception:
                LOGGER.exception(
                    "EVBox accepted the active TxProfile but the TxDefaultProfile "
                    "update for the next transaction failed"
                )
        else:
            default_profile = self._current_profile(
                amperes,
                profile_id=2001,
                purpose="TxDefaultProfile",
                stack_level=0,
            )
            response = await self._require_connection().call(
                "SetChargingProfile",
                {"connectorId": 1, "csChargingProfiles": default_profile},
            )
            if not _is_accepted(response):
                raise MqttCommandRejected(
                    f"EVBox rejected SetChargingProfile(TxDefaultProfile): {response}"
                )
            LOGGER.info("EVBox accepted %.1f A TxDefaultProfile", amperes)

        self._store.state.maximum_current = amperes
        self._store.save()
        self._mqtt.publish_maximum_current(amperes)

    @staticmethod
    def _current_profile(
        amperes: float,
        *,
        profile_id: int,
        purpose: str,
        stack_level: int,
        transaction_id: int | None = None,
    ) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "chargingProfileId": profile_id,
            "stackLevel": stack_level,
            "chargingProfilePurpose": purpose,
            "chargingProfileKind": "Relative",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": amperes}],
            },
        }
        if transaction_id is not None:
            profile["transactionId"] = transaction_id
        return profile

    def _require_connection(self) -> OcppConnection:
        connection = self._connection
        if connection is None or connection.closed:
            raise ConnectionError("EVBox Elvi is not connected through OCPP")
        return connection


def extract_power_kw(payload: dict[str, Any], number_of_phases: int) -> float | None:
    """Extract Power.Active.Import or derive it from standardized current/voltage samples."""
    samples = _latest_samples(payload)

    power_values = [
        _to_watts(sample)
        for sample in samples
        if sample.get("measurand", "Energy.Active.Import.Register") == "Power.Active.Import"
    ]
    valid_power_values = [value for value in power_values if value is not None]
    if valid_power_values:
        return sum(valid_power_values) / 1000.0

    currents = _values_by_phase(samples, "Current.Import")
    voltages = _values_by_phase(samples, "Voltage")
    if not currents:
        return None
    phased_currents = {phase: value for phase, value in currents.items() if phase}
    phased_voltages = {phase: value for phase, value in voltages.items() if phase}
    if phased_currents and phased_voltages:
        total_watts = 0.0
        matches = 0
        for phase, current in phased_currents.items():
            voltage = phased_voltages.get(phase)
            if voltage is None:
                voltage = _voltage_for_current_phase(phase, phased_voltages)
            if voltage is not None:
                total_watts += current * voltage
                matches += 1
        if matches:
            return total_watts / 1000.0

    current = currents.get("")
    if current is None:
        current = sum(phased_currents.values()) / max(len(phased_currents), 1)
    voltage = voltages.get("")
    if voltage is None and phased_voltages:
        voltage = sum(phased_voltages.values()) / len(phased_voltages)
    voltage = voltage or 230.0
    return current * voltage * number_of_phases / 1000.0


def extract_current_a(payload: dict[str, Any]) -> float | None:
    """Extract the latest measured Current.Import value in amperes.

    An explicit value without a phase is authoritative. If the charge point
    reports individual phases, use the average of the active phases, matching
    the amperes-per-phase meaning of an AC charging limit. All-zero phase
    samples produce zero rather than no reading.
    """
    samples = _latest_samples(payload)
    currents = _values_by_phase(
        [sample for sample in samples if sample.get("unit", "A") == "A"],
        "Current.Import",
    )
    aggregate = currents.get("")
    if aggregate is not None:
        return max(0.0, aggregate)

    phase_values = [
        value
        for phase, value in currents.items()
        if phase in {"L1", "L2", "L3"}
    ]
    if not phase_values:
        return None
    active_phase_values = [value for value in phase_values if value > 0]
    if not active_phase_values:
        return 0.0
    return sum(active_phase_values) / len(active_phase_values)


def _latest_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    meter_values = [
        meter_value
        for meter_value in payload.get("meterValue", [])
        if isinstance(meter_value, dict)
    ]
    if not meter_values:
        return []
    return [
        sample
        for sample in meter_values[-1].get("sampledValue", [])
        if isinstance(sample, dict)
    ]


def _values_by_phase(samples: list[dict[str, Any]], measurand: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for sample in samples:
        if sample.get("measurand") != measurand:
            continue
        try:
            value = float(sample["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values[str(sample.get("phase", ""))] = value
    return values


def _voltage_for_current_phase(phase: str, voltages: dict[str, float]) -> float | None:
    candidates = {
        "L1": "L1-N",
        "L2": "L2-N",
        "L3": "L3-N",
        "L1-N": "L1-N",
        "L2-N": "L2-N",
        "L3-N": "L3-N",
    }
    return voltages.get(candidates.get(phase, ""))


def _to_watts(sample: dict[str, Any]) -> float | None:
    try:
        value = float(sample["value"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = str(sample.get("unit", "W"))
    if unit in {"kW", "kVA"}:
        return value * 1000
    return value


def _is_accepted(
    response: dict[str, Any],
    *,
    accepted_values: set[str] | None = None,
) -> bool:
    accepted = accepted_values or {ACCEPTED}
    return str(response.get("status", "")) in accepted


def _integer(value: Any, *, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string(value: Any) -> str | None:
    if value is None:
        return None
    converted = str(value).strip()
    return converted or None
