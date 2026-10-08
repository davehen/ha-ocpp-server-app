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
        self._needs_initialization = False
        self._status: str | None = None
        self._charge_control: bool | None = None
        self._transaction_id: int | None = None
        self._session_known = False
        self._epoch = 0
        self._current_confirmed = False
        self._power_kw: float | None = None
        self._current_a: float | None = None
        self._start_pending = False
        self._stop_pending = False
        self._pending_until = 0.0
        self._sync_task: asyncio.Task[None] | None = None
        self._last_status_time: datetime | None = None
        self._last_meter_time: datetime | None = None
        self._store.load()
        mqtt_bridge.set_command_handler(self.handle_mqtt_command)

    def start(self) -> None:
        """Publish retained initial states after MQTT is connected."""
        self._mqtt.publish_charger_online(False)
        self._reset_live_state()

    def _reset_live_state(self) -> None:
        """Invalidate observations together; a saved setpoint is not an observation."""
        self._epoch += 1
        self._status = None
        self._charge_control = None
        self._transaction_id = None
        self._session_known = False
        self._current_confirmed = False
        self._power_kw = None
        self._current_a = None
        self._start_pending = False
        self._stop_pending = False
        self._pending_until = 0.0
        self._last_status_time = None
        self._last_meter_time = None
        self._mqtt.publish_maximum_current(None)
        self._publish_connector_state()
        self._mqtt.publish_power(None)
        self._mqtt.publish_current(None)

    def _publish_connector_state(self) -> None:
        """Reconcile both switches from connector status and session evidence."""
        availability = (
            self._status == "Available"
            if self._status is not None
            else False
            if self._transaction_id is not None
            else None
        )
        if self._status in {"Faulted", "Unavailable", "Reserved"} and self._transaction_id is None:
            availability = None
        charge_control = self._charge_control
        if not self._session_known or not self._current_confirmed:
            charge_control = None
        elif charge_control and (self._transaction_id is None or self._power_kw is None):
            charge_control = None
        self._mqtt.publish_charge_control(charge_control)
        self._mqtt.publish_charger_availability(availability)

    async def attach(self, connection: OcppConnection) -> None:
        """Attach the newest charge point connection, replacing a stale one."""
        previous = self._connection
        if self._initialization_task is not None:
            self._initialization_task.cancel()
        if self._sync_task is not None:
            self._sync_task.cancel()
        self._connection = connection
        self._needs_initialization = True
        self._reset_live_state()
        self._mqtt.publish_charger_online(True)
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
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        self._mqtt.publish_charger_online(False)

    async def handle_ocpp_call(
        self, action: str, payload: dict[str, Any], *, connection: OcppConnection | None = None
    ) -> dict[str, Any]:
        """Return the OCPP 1.6 response for one charge-point initiated call."""
        if connection is not None and connection is not self._connection:
            raise ConnectionError("Ignoring a message from a retired OCPP connection")
        self._expire_pending()
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

    async def after_ocpp_call(
        self, action: str, *, connection: OcppConnection | None = None
    ) -> None:
        """Run post-response work without blocking the WebSocket receive loop."""
        if connection is not None and connection is not self._connection:
            return
        if self._can_synchronize() and (self._sync_task is None or self._sync_task.done()):
            self._sync_task = asyncio.create_task(
                self._synchronize_current_safely(), name="synchronize-elvi-current"
            )
        if action != "BootNotification" and not self._needs_initialization:
            return
        self._needs_initialization = False
        if self._initialization_task is not None:
            self._initialization_task.cancel()
        self._initialization_task = asyncio.create_task(
            self._initialize_charger_safely(),
            name="initialize-evbox-elvi",
        )

    async def handle_mqtt_command(self, topic: str, payload: str) -> None:
        """Execute one command requested by a Home Assistant MQTT entity."""
        connection = self._require_connection()
        epoch = self._epoch
        async with self._command_lock:
            if connection is not self._connection or connection.closed or epoch != self._epoch:
                raise MqttCommandRejected(
                    "Discarding a command queued on a retired OCPP connection"
                )
            self._expire_pending()
            if topic == self._mqtt.topic(f"{MqttBridge.CHARGE_CONTROL}/set"):
                if payload.upper() == "ON":
                    await self._start_charging()
                elif payload.upper() == "OFF":
                    await self._stop_charging()
                else:
                    raise InvalidMqttCommand(f"Invalid charge-control payload: {payload!r}")
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
        if self._sync_task is not None:
            self._sync_task.cancel()
        self._reset_live_state()
        self._mqtt.update_device_information(
            manufacturer=_string(payload.get("chargePointVendor")),
            model=_string(payload.get("chargePointModel")),
            serial_number=_string(
                payload.get("chargePointSerialNumber") or payload.get("chargeBoxSerialNumber")
            ),
            firmware_version=_string(payload.get("firmwareVersion")),
        )
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
        if connector_id != 1:
            return {}
        status = _string(payload.get("status"))
        if not status:
            return {}
        if status not in ZERO_POWER_STATUSES | CHARGING_STATUSES:
            LOGGER.warning("Ignoring unknown connector status: %s", status)
            return {}
        timestamp = _timestamp(payload.get("timestamp"))
        if (
            timestamp is not None
            and self._last_status_time is not None
            and timestamp < self._last_status_time
        ):
            LOGGER.warning("Ignoring an older connector status")
            return {}
        if timestamp is not None:
            self._last_status_time = timestamp
        self._status = status
        if status in CHARGING_STATUSES:
            self._charge_control = True
            self._session_known = True
        elif status in {"Available", "Unavailable", "Faulted", "Finishing"}:
            self._charge_control = False
            self._session_known = True
            if status in {"Available", "Finishing"}:
                self._finish_transaction()
        elif status in {"Preparing", "Reserved"}:
            self._charge_control = self._transaction_id is not None
            self._session_known = not self._start_pending or self._transaction_id is not None
        if status in ZERO_POWER_STATUSES:
            self._publish_measurements(0, 0)
        self._publish_connector_state()
        return {}

    def _on_start_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        if _integer(payload.get("connectorId", 1), default=None) != 1:
            raise InvalidMqttCommand("Only connector 1 is supported")
        if self._transaction_id is not None:
            raise MqttCommandRejected("A transaction is already active on connector 1")
        now = int(time.time())
        transaction_id = max(now, self._store.state.last_transaction_id + 1)
        self._store.state.last_transaction_id = transaction_id
        self._store.save()
        self._transaction_id = transaction_id
        self._session_known = True
        self._current_confirmed = False
        self._start_pending = False
        self._charge_control = True
        self._status = None
        self._power_kw = self._current_a = None
        self._mqtt.publish_power(None)
        self._mqtt.publish_current(None)
        self._publish_connector_state()
        return {"transactionId": transaction_id, "idTagInfo": {"status": ACCEPTED}}

    def _on_stop_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = _integer(payload.get("transactionId"), default=None)
        if transaction_id is None or transaction_id != self._transaction_id:
            if (
                self._transaction_id is None
                and transaction_id is not None
                and 0 <= transaction_id <= self._store.state.last_transaction_id
            ):
                self._store.state.last_closed_transaction_id = max(
                    self._store.state.last_closed_transaction_id, transaction_id
                )
                self._save_state_safely()
            LOGGER.info(
                "Acknowledging StopTransaction for non-active transaction %s", transaction_id
            )
            return {"idTagInfo": {"status": ACCEPTED}}
        self._finish_transaction()
        self._charge_control = False
        self._status = None
        self._publish_connector_state()
        self._publish_measurements(0, 0)
        return {"idTagInfo": {"status": ACCEPTED}}

    def _on_meter_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        if _integer(payload.get("connectorId"), default=0) != 1:
            return {}
        timestamp = _latest_meter_time(payload)
        if (
            timestamp is not None
            and self._last_meter_time is not None
            and timestamp < self._last_meter_time
        ):
            LOGGER.warning("Ignoring older meter values")
            return {}
        transaction_id = payload.get("transactionId")
        if transaction_id is not None and _integer(payload.get("connectorId"), default=0) == 1:
            recovered_id = _integer(transaction_id, default=None)
            if recovered_id is not None and recovered_id >= 0:
                if (
                    recovered_id <= self._store.state.last_closed_transaction_id
                    or (self._transaction_id is not None and self._transaction_id != recovered_id)
                    or (self._session_known and self._charge_control is False)
                ):
                    LOGGER.info("Ignoring meter values for non-active transaction %s", recovered_id)
                    return {}
                self._transaction_id = recovered_id
                self._store.state.last_transaction_id = max(
                    self._store.state.last_transaction_id, recovered_id
                )
                self._session_known = True
                self._charge_control = True
        if timestamp is not None:
            self._last_meter_time = timestamp
        power_kw = extract_power_kw(payload, self._config.number_of_phases)
        if power_kw is not None:
            self._power_kw = power_kw
            self._mqtt.publish_power(power_kw)
        current_a = extract_current_a(payload)
        if current_a is not None:
            self._current_a = current_a
            self._mqtt.publish_current(current_a)
        self._publish_connector_state()
        return {}

    def _publish_measurements(self, power: float, current: float) -> None:
        self._power_kw, self._current_a = power, current
        self._mqtt.publish_power(power)
        self._mqtt.publish_current(current)

    def _save_state_safely(self) -> None:
        try:
            self._store.save()
        except OSError:
            LOGGER.exception("Could not persist acknowledged state; live state remains valid")

    def _finish_transaction(self) -> None:
        if self._transaction_id is not None:
            self._store.state.last_closed_transaction_id = max(
                self._store.state.last_closed_transaction_id,
                self._transaction_id,
            )
            self._save_state_safely()
            self._current_confirmed = False
            self._mqtt.publish_maximum_current(None)
        self._transaction_id = None
        self._session_known = True
        self._start_pending = self._stop_pending = False

    def _can_synchronize(self) -> bool:
        return (
            self._connection is not None
            and self._session_known
            and not self._current_confirmed
            and getattr(self._store, "valid", True)
            and (self._charge_control is False or self._transaction_id is not None)
        )

    def _expire_pending(self) -> None:
        """Permit an explicit retry if an accepted start/stop never completes."""
        if (self._start_pending or self._stop_pending) and time.monotonic() >= self._pending_until:
            LOGGER.warning("Accepted start/stop did not complete within the command timeout")
            self._start_pending = self._stop_pending = False
            if self._status == "Preparing" and self._transaction_id is None:
                self._session_known = True
                self._charge_control = False
                self._publish_connector_state()

    async def _synchronize_current_safely(self) -> None:
        try:
            async with self._command_lock:
                if not self._can_synchronize():
                    return
                await self._set_current(self._store.state.maximum_current)
                if self._charge_control and self._power_kw is None:
                    response = await self._require_connection().call(
                        "TriggerMessage",
                        {"requestedMessage": "MeterValues", "connectorId": 1},
                    )
                    if not _is_accepted(response):
                        LOGGER.warning("Meter refresh rejected; waiting for normal meter cadence")
        except asyncio.CancelledError:
            raise
        except (MqttCommandRejected, ConnectionError, TimeoutError) as error:
            LOGGER.warning("Current synchronization incomplete: %s", error)
        except Exception:
            LOGGER.exception("Current synchronization failed; OCPP service remains active")

    @staticmethod
    def _on_data_transfer(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": ACCEPTED}

    @staticmethod
    def _on_empty_notification(payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _initialize_charger_safely(self) -> None:
        try:
            connection = self._require_connection()
            response = await connection.call(
                "TriggerMessage",
                {"requestedMessage": "StatusNotification", "connectorId": 1},
            )
            if not _is_accepted(response):
                LOGGER.warning("EVBox rejected TriggerMessage(StatusNotification): %s", response)
            if connection is not self._connection:
                return
            if self._config.configure_meter_values:
                await self._ensure_configuration(
                    {
                        "MeterValuesSampledData": SUPPORTED_METER_VALUES,
                        "MeterValueSampleInterval": str(self._config.meter_value_interval),
                    },
                    connection=connection,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Best-effort EVBox initialization failed; OCPP service remains active")

    async def _ensure_configuration(
        self, desired: dict[str, str], *, connection: OcppConnection | None = None
    ) -> None:
        connection = connection or self._require_connection()
        response = await connection.call(
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
            if connection is not self._connection:
                return
            current = current_by_key.get(key)
            if current is None:
                LOGGER.warning("EVBox did not report configuration key %s", key)
                continue
            if str(current.get("value", "")) == desired_value:
                continue
            if current.get("readonly") is True:
                LOGGER.warning("EVBox configuration key %s is read-only", key)
                continue
            await self._change_configuration(key, desired_value, connection=connection)

    async def _change_configuration(
        self, key: str, value: str, *, connection: OcppConnection | None = None
    ) -> None:
        connection = connection or self._require_connection()
        response = await connection.call(
            "ChangeConfiguration",
            {"key": key, "value": value},
        )
        if not _is_accepted(response):
            LOGGER.warning("EVBox rejected ChangeConfiguration(%s): %s", key, response)

    async def _start_charging(self) -> None:
        if not self._session_known or not self._current_confirmed:
            raise MqttCommandRejected("Cannot start before connector and current synchronization")
        if self._charge_control or self._start_pending:
            raise MqttCommandRejected("A transaction is active or start is already pending")
        if self._status in {"Available", "Unavailable", "Faulted", "Reserved", "Finishing"}:
            raise MqttCommandRejected("Connector is not ready for remote start")
        connection = self._require_connection()
        epoch = self._epoch
        amperes = self._store.state.maximum_current
        self._validate_current(amperes)
        self._start_pending = True
        self._pending_until = time.monotonic() + self._config.command_timeout
        try:
            response = await connection.call(
                "RemoteStartTransaction",
                {
                    "idTag": self._config.id_tag,
                    "connectorId": 1,
                    "chargingProfile": self._current_profile(
                        amperes,
                        profile_id=2002,
                        purpose="TxProfile",
                        stack_level=1,
                    ),
                },
            )
        except BaseException:
            if connection is self._connection and epoch == self._epoch:
                self._start_pending = False
            raise
        if connection is not self._connection or epoch != self._epoch:
            raise MqttCommandRejected("Remote start response belongs to a retired connection")
        if not _is_accepted(response):
            self._start_pending = False
            raise MqttCommandRejected(f"EVBox rejected RemoteStartTransaction: {response}")
        LOGGER.info("EVBox accepted RemoteStartTransaction with %.1f A TxProfile", amperes)

    async def _stop_charging(self) -> None:
        if self._transaction_id is None:
            raise MqttCommandRejected("Cannot stop charging before an OCPP transaction ID is known")
        if self._stop_pending:
            raise MqttCommandRejected("Remote stop is already pending")
        connection = self._require_connection()
        epoch = self._epoch
        transaction_id = self._transaction_id
        response = await connection.call(
            "RemoteStopTransaction",
            {"transactionId": transaction_id},
        )
        if not _is_accepted(response):
            raise MqttCommandRejected(f"EVBox rejected RemoteStopTransaction: {response}")
        if (
            connection is self._connection
            and epoch == self._epoch
            and transaction_id == self._transaction_id
        ):
            self._stop_pending = True
            self._pending_until = time.monotonic() + self._config.command_timeout

    async def _set_availability(self, available: bool) -> None:
        response = await self._require_connection().call(
            "ChangeAvailability",
            {"connectorId": 1, "type": "Operative" if available else "Inoperative"},
        )
        if not _is_accepted(response, accepted_values={"Accepted", "Scheduled"}):
            raise MqttCommandRejected(f"EVBox rejected ChangeAvailability: {response}")

    def _validate_current(self, amperes: float) -> None:
        if not math.isfinite(amperes) or amperes < 0 or amperes > self._config.maximum_current:
            raise InvalidMqttCommand(
                f"Current must be between 0 and {self._config.maximum_current:g} A"
            )

    async def _set_current(self, amperes: float) -> None:
        self._validate_current(amperes)
        if not self._session_known or (self._charge_control and self._transaction_id is None):
            raise MqttCommandRejected(
                "Cannot apply current before the active transaction is recovered"
            )
        connection = self._require_connection()
        epoch = self._epoch
        transaction_id = self._transaction_id
        if self._current_confirmed and amperes == self._store.state.maximum_current:
            return
        if transaction_id is not None:
            transaction_profile = self._current_profile(
                amperes,
                profile_id=2002,
                purpose="TxProfile",
                stack_level=1,
                transaction_id=transaction_id,
            )
            response = await connection.call(
                "SetChargingProfile",
                {"connectorId": 1, "csChargingProfiles": transaction_profile},
            )
            if not _is_accepted(response):
                raise MqttCommandRejected(
                    f"EVBox rejected SetChargingProfile(TxProfile): {response}"
                )
            self._record_current(connection, epoch, transaction_id, amperes)
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
                default_response = await connection.call(
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
            except (ConnectionError, TimeoutError) as error:
                LOGGER.warning("Active limit accepted; default profile update failed: %s", error)
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
            response = await connection.call(
                "SetChargingProfile",
                {"connectorId": 1, "csChargingProfiles": default_profile},
            )
            if not _is_accepted(response):
                raise MqttCommandRejected(
                    f"EVBox rejected SetChargingProfile(TxDefaultProfile): {response}"
                )
            self._record_current(connection, epoch, transaction_id, amperes)
            LOGGER.info("EVBox accepted %.1f A TxDefaultProfile", amperes)

    def _record_current(
        self, connection: OcppConnection, epoch: int, transaction_id: int | None, amperes: float
    ) -> None:
        if (
            connection is not self._connection
            or connection.closed
            or epoch != self._epoch
            or transaction_id != self._transaction_id
            or (transaction_id is None and self._charge_control is not False)
        ):
            raise MqttCommandRejected("Discarding current acknowledgment for a retired session")
        self._store.state.maximum_current = amperes
        self._store.valid = True
        self._save_state_safely()
        self._current_confirmed = True
        self._mqtt.publish_maximum_current(amperes)
        self._publish_connector_state()

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

    power_values = {}
    for sample in samples:
        if sample.get("measurand") != "Power.Active.Import":
            continue
        watts = _to_watts(sample)
        if watts is not None:
            phase = str(sample.get("phase", "")).removesuffix("-N")
            if phase in {"", "L1", "L2", "L3"}:
                power_values[phase] = watts
    if "" in power_values:
        return power_values[""] / 1000.0
    valid_power_values = list(power_values.values())
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

    phase_values = [value for phase, value in currents.items() if phase in {"L1", "L2", "L3"}]
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
        sample for sample in meter_values[-1].get("sampledValue", []) if isinstance(sample, dict)
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
        if math.isfinite(value) and value >= 0:
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
    if not math.isfinite(value) or value < 0 or unit not in {"W", "kW"}:
        return None
    if unit == "kW":
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
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(UTC) if timestamp.tzinfo is not None else None
    except ValueError:
        return None


def _latest_meter_time(payload: dict[str, Any]) -> datetime | None:
    values = payload.get("meterValue")
    if isinstance(values, list) and values and isinstance(values[-1], dict):
        return _timestamp(values[-1].get("timestamp"))
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    converted = str(value).strip()
    return converted or None
