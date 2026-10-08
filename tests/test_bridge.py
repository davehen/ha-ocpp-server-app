from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from app.bridge import BridgeController, extract_current_a, extract_power_kw
from app.config import Config
from app.mqtt import InvalidMqttCommand, MqttBridge, MqttCommandRejected
from app.state import PersistentState


class FakeMqtt:
    CHARGE_CONTROL = MqttBridge.CHARGE_CONTROL
    MAXIMUM_CURRENT = MqttBridge.MAXIMUM_CURRENT
    CHARGER_AVAILABILITY = MqttBridge.CHARGER_AVAILABILITY

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.command_handler = None
        self.device_information: dict[str, Any] = {}

    def topic(self, suffix: str) -> str:
        return f"evbox_elvi/{suffix}"

    def set_command_handler(self, handler) -> None:
        self.command_handler = handler

    def publish_charger_online(self, online: bool) -> None:
        self.values["online"] = online

    def publish_charge_control(self, enabled: bool | None) -> None:
        self.values["charge_control"] = enabled

    def publish_charger_availability(self, enabled: bool | None) -> None:
        self.values["availability"] = enabled

    def publish_power(self, kilowatts: float | None) -> None:
        self.values["power"] = kilowatts

    def publish_current(self, amperes: float | None) -> None:
        self.values["current"] = amperes

    def publish_maximum_current(self, amperes: float) -> None:
        self.values["maximum_current"] = amperes

    def update_device_information(self, **values: Any) -> None:
        self.device_information = values


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.response_sequences: dict[str, list[dict[str, Any]]] = {}

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, payload))
        sequence = self.response_sequences.get(action)
        if sequence:
            return sequence.pop(0)
        return self.responses.get(action, {"status": "Accepted"})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class FakeStateStore:
    def __init__(self, default_current: float) -> None:
        self.state = PersistentState(maximum_current=default_current)

    def load(self) -> PersistentState:
        return self.state

    def save(self) -> None:
        return None


class BridgeControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_invalidates_all_observations_not_saved_limit(self) -> None:
        self.bridge.start()
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Available"}
        )
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.bridge.detach(self.connection)
        await self.bridge.attach(FakeConnection())
        for key in ("charge_control", "availability", "power", "current"):
            self.assertIsNone(self.mqtt.values[key], key)
        self.assertIsNone(self.mqtt.values["maximum_current"])
        self.assertEqual(self.bridge._store.state.maximum_current, 8)  # noqa: SLF001
        await self.bridge.handle_ocpp_call("Heartbeat", {})
        self.assertIsNone(self.mqtt.values["availability"])
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Available"}
        )
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertTrue(self.mqtt.values["availability"])
        self.assertFalse(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["power"], 0)

    async def test_start_and_stop_reconcile_connector_without_guessing_unplugged(self) -> None:
        await self.bridge.attach(self.connection)
        response = await self.bridge.handle_ocpp_call("StartTransaction", {})
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "SuspendedEVSE"}
        )
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertFalse(self.mqtt.values["availability"])
        self.assertTrue(self.mqtt.values["charge_control"])
        await self.bridge.handle_ocpp_call(
            "StopTransaction", {"transactionId": response["transactionId"]}
        )
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertIsNone(self.mqtt.values["availability"])
        self.assertFalse(self.mqtt.values["charge_control"])

    async def test_unknown_status_does_not_create_connector_state(self) -> None:
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "invalid"}
        )
        self.assertIsNone(self.mqtt.values["availability"])

    def setUp(self) -> None:
        self.config = Config(
            mqtt_host="mqtt",
            mqtt_port=1883,
            mqtt_username="",
            mqtt_password="",
            expected_charge_point_id="",
            id_tag="HomeAssistant",
            heartbeat_interval=300,
            meter_value_interval=60,
            configure_meter_values=False,
            maximum_current=16,
            number_of_phases=3,
            command_timeout=20,
            log_level="INFO",
            data_directory=Path("/unused"),
        )
        self.mqtt = FakeMqtt()
        store = FakeStateStore(self.config.maximum_current)
        self.bridge = BridgeController(self.config, self.mqtt, store)  # type: ignore[arg-type]
        self.connection = FakeConnection()

    async def prepare_idle(self, current: float | None = None) -> None:
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        if current is not None:
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", str(current))
            self.connection.calls.clear()

    async def test_reconnect_without_boot_notification_restores_availability(self) -> None:
        self.bridge.start()
        self.assertFalse(self.mqtt.values["online"])
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        self.assertTrue(self.mqtt.values["online"])

        self.bridge.detach(self.connection)  # type: ignore[arg-type]
        self.assertFalse(self.mqtt.values["online"])

        replacement = FakeConnection()
        await self.bridge.attach(replacement)  # type: ignore[arg-type]
        await self.bridge.handle_ocpp_call("Heartbeat", {})
        self.assertTrue(self.mqtt.values["online"])

        # Cleanup of the old socket must not mark the new connection offline.
        self.bridge.detach(self.connection)  # type: ignore[arg-type]
        self.assertTrue(self.mqtt.values["online"])
        self.bridge.detach(replacement)  # type: ignore[arg-type]
        self.assertFalse(self.mqtt.values["online"])

    async def test_boot_and_status_publish_expected_states(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]

        response = await self.bridge.handle_ocpp_call(
            "BootNotification",
            {
                "chargePointVendor": "EV-BOX",
                "chargePointModel": "G4E-WBO-M5320E",
                "chargePointSerialNumber": "EVB-P123",
                "firmwareVersion": "1.2.3",
            },
        )
        self.assertEqual(response["status"], "Accepted")
        self.assertEqual(response["interval"], 300)
        self.assertTrue(self.mqtt.values["online"])
        self.assertEqual(self.mqtt.device_information["serial_number"], "EVB-P123")

        await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "status": "Preparing", "errorCode": "NoError"},
        )
        self.assertFalse(self.mqtt.values["availability"])
        self.assertIsNone(self.mqtt.values["charge_control"])

        await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "status": "Charging", "errorCode": "NoError"},
        )
        self.assertIsNone(self.mqtt.values["charge_control"])

        self.mqtt.values["power"] = 2.5
        self.mqtt.values["current"] = 8.0
        await self.bridge.handle_ocpp_call("MeterValues", {"connectorId": 1, "transactionId": 123})
        await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "status": "SuspendedEVSE", "errorCode": "NoError"},
        )
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["power"], 0)
        self.assertEqual(self.mqtt.values["current"], 0)

    async def test_meter_values_publish_measured_current_and_power(self) -> None:
        await self.bridge.handle_ocpp_call(
            "MeterValues",
            {
                "connectorId": 1,
                "meterValue": [
                    {
                        "sampledValue": [
                            {"value": "10", "measurand": "Current.Import", "unit": "A"},
                            {
                                "value": "2300",
                                "measurand": "Power.Active.Import",
                                "unit": "W",
                            },
                        ]
                    }
                ],
            },
        )

        self.assertEqual(self.mqtt.values["current"], 10.0)
        self.assertEqual(self.mqtt.values["power"], 2.3)

    async def test_idle_current_command_uses_tx_default_profile(self) -> None:
        await self.prepare_idle()

        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8.5")

        action, payload = self.connection.calls[-1]
        self.assertEqual(action, "SetChargingProfile")
        self.assertEqual(payload["connectorId"], 1)
        profile = payload["csChargingProfiles"]
        self.assertEqual(profile["chargingProfileId"], 2001)
        self.assertEqual(profile["stackLevel"], 0)
        self.assertEqual(profile["chargingProfilePurpose"], "TxDefaultProfile")
        self.assertEqual(profile["chargingProfileKind"], "Relative")
        self.assertNotIn("transactionId", profile)
        self.assertEqual(
            profile["chargingSchedule"]["chargingSchedulePeriod"],
            [{"startPeriod": 0, "limit": 8.5}],
        )
        self.assertEqual(self.mqtt.values["maximum_current"], 8.5)

    async def test_active_current_command_uses_transaction_profile_then_default(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        start_response = await self.bridge.handle_ocpp_call(
            "StartTransaction",
            {
                "connectorId": 1,
                "idTag": "HomeAssistant",
                "meterStart": 10,
                "timestamp": "now",
            },
        )

        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")

        self.assertEqual(len(self.connection.calls), 2)
        active_action, active_payload = self.connection.calls[0]
        self.assertEqual(active_action, "SetChargingProfile")
        self.assertEqual(active_payload["connectorId"], 1)
        active_profile = active_payload["csChargingProfiles"]
        self.assertEqual(active_profile["chargingProfileId"], 2002)
        self.assertEqual(active_profile["stackLevel"], 1)
        self.assertEqual(active_profile["chargingProfilePurpose"], "TxProfile")
        self.assertEqual(active_profile["transactionId"], start_response["transactionId"])
        self.assertEqual(
            active_profile["chargingSchedule"]["chargingSchedulePeriod"],
            [{"startPeriod": 0, "limit": 8.0}],
        )

        default_action, default_payload = self.connection.calls[1]
        self.assertEqual(default_action, "SetChargingProfile")
        default_profile = default_payload["csChargingProfiles"]
        self.assertEqual(default_profile["chargingProfileId"], 2001)
        self.assertEqual(default_profile["stackLevel"], 0)
        self.assertEqual(default_profile["chargingProfilePurpose"], "TxDefaultProfile")
        self.assertNotIn("transactionId", default_profile)
        self.assertEqual(self.mqtt.values["maximum_current"], 8.0)

    async def test_five_amp_pause_value_is_forwarded_unchanged(self) -> None:
        await self.prepare_idle()

        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "5")

        profile = self.connection.calls[-1][1]["csChargingProfiles"]
        periods = profile["chargingSchedule"]["chargingSchedulePeriod"]
        self.assertEqual(periods, [{"startPeriod": 0, "limit": 5.0}])
        self.assertEqual(self.mqtt.values["maximum_current"], 5.0)

    async def test_meter_configuration_changes_only_different_writable_value(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        self.connection.responses["GetConfiguration"] = {
            "configurationKey": [
                {
                    "key": "MeterValuesSampledData",
                    "readonly": False,
                    "value": "Energy.Active.Import.Register",
                },
                {
                    "key": "MeterValueSampleInterval",
                    "readonly": True,
                    "value": "30",
                },
            ]
        }

        await self.bridge._ensure_configuration(  # noqa: SLF001
            {
                "MeterValuesSampledData": "Power.Active.Import",
                "MeterValueSampleInterval": "60",
            }
        )

        self.assertEqual(
            self.connection.calls,
            [
                (
                    "GetConfiguration",
                    {"key": ["MeterValuesSampledData", "MeterValueSampleInterval"]},
                ),
                (
                    "ChangeConfiguration",
                    {"key": "MeterValuesSampledData", "value": "Power.Active.Import"},
                ),
            ],
        )

    async def test_rejected_current_does_not_publish_or_persist(self) -> None:
        await self.prepare_idle()
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}

        with self.assertRaisesRegex(
            RuntimeError,
            r"rejected SetChargingProfile\(TxDefaultProfile\)",
        ):
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "7")

        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_rejected_active_transaction_profile_is_not_reported_as_applied(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        await self.bridge.handle_ocpp_call(
            "StartTransaction",
            {
                "connectorId": 1,
                "idTag": "HomeAssistant",
                "meterStart": 10,
                "timestamp": "now",
            },
        )
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}

        with self.assertRaisesRegex(
            RuntimeError,
            r"rejected SetChargingProfile\(TxProfile\)",
        ):
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "7")

        self.assertEqual(len(self.connection.calls), 1)
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_rejected_default_follow_up_does_not_undo_active_limit(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        await self.bridge.handle_ocpp_call(
            "StartTransaction",
            {
                "connectorId": 1,
                "idTag": "HomeAssistant",
                "meterStart": 10,
                "timestamp": "now",
            },
        )
        self.connection.response_sequences["SetChargingProfile"] = [
            {"status": "Accepted"},
            {"status": "Rejected"},
        ]

        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "7")

        self.assertEqual(len(self.connection.calls), 2)
        self.assertEqual(self.mqtt.values["maximum_current"], 7.0)

    async def test_remote_start_and_stop_use_wallbox_transaction(self) -> None:
        await self.prepare_idle(8)

        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        action, payload = self.connection.calls[-1]
        self.assertEqual(action, "RemoteStartTransaction")
        self.assertEqual(payload["idTag"], "HomeAssistant")
        self.assertEqual(payload["connectorId"], 1)
        self.assertEqual(payload["chargingProfile"]["chargingProfilePurpose"], "TxProfile")
        self.assertNotIn("transactionId", payload["chargingProfile"])
        start_response = await self.bridge.handle_ocpp_call(
            "StartTransaction",
            {
                "connectorId": 1,
                "idTag": "HomeAssistant",
                "meterStart": 10,
                "timestamp": "now",
            },
        )

        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "OFF")
        self.assertEqual(
            self.connection.calls[-1],
            ("RemoteStopTransaction", {"transactionId": start_response["transactionId"]}),
        )
        self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_invalid_charge_control_payload_is_an_expected_command_error(self) -> None:
        await self.bridge.attach(self.connection)
        with self.assertRaisesRegex(InvalidMqttCommand, "Invalid charge-control payload"):
            await self.bridge.handle_mqtt_command(
                "evbox_elvi/charge_control/set",
                "ONbh",
            )

    async def test_rejected_remote_start_is_an_expected_command_error(self) -> None:
        await self.prepare_idle(8)
        self.connection.responses["RemoteStartTransaction"] = {"status": "Rejected"}

        with self.assertRaisesRegex(MqttCommandRejected, "rejected RemoteStartTransaction"):
            await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")

        self.assertFalse(self.mqtt.values["charge_control"])

    async def test_start_uses_persisted_limit_after_server_restart(self) -> None:
        self.bridge._store.state.maximum_current = 8  # noqa: SLF001
        self.bridge.start()
        await self.prepare_idle(8)
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")

        profile = self.connection.calls[-1][1]["chargingProfile"]
        self.assertEqual(profile["stackLevel"], 1)
        self.assertEqual(profile["chargingProfileKind"], "Relative")
        self.assertEqual(profile["chargingSchedule"]["chargingRateUnit"], "A")
        self.assertEqual(
            profile["chargingSchedule"]["chargingSchedulePeriod"],
            [{"startPeriod": 0, "limit": 8}],
        )

    async def test_start_does_not_fall_back_when_charging_profile_is_rejected(self) -> None:
        await self.prepare_idle(8)
        self.connection.responses["RemoteStartTransaction"] = {"status": "Rejected"}
        with self.assertRaises(MqttCommandRejected):
            await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        self.assertEqual(len(self.connection.calls), 1)
        self.assertFalse(self.mqtt.values["charge_control"])

    async def test_reconnected_transaction_restores_on_even_when_suspended(self) -> None:
        self.bridge.start()
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        self.assertIsNone(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["availability"])
        await self.bridge.handle_ocpp_call(
            "MeterValues",
            {
                "connectorId": 1,
                "transactionId": 123,
                "meterValue": [
                    {
                        "sampledValue": [
                            {"measurand": "Current.Import", "value": "0", "unit": "A"},
                        ]
                    }
                ],
            },
        )
        self.assertIsNone(self.mqtt.values["charge_control"])
        self.assertFalse(self.mqtt.values["availability"])
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertEqual(self.connection.calls[0][1]["csChargingProfiles"]["transactionId"], 123)
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "OFF")
        self.assertEqual(
            self.connection.calls[-1], ("RemoteStopTransaction", {"transactionId": 123})
        )

    async def test_meter_without_valid_transaction_does_not_invent_session(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        for payload in (
            {"connectorId": 1},
            {"connectorId": 1, "transactionId": "invalid"},
            {"connectorId": 1, "transactionId": -1},
            {"connectorId": 0, "transactionId": 123},
        ):
            await self.bridge.handle_ocpp_call("MeterValues", payload)
            self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_reconnect_requests_status_after_first_response_without_boot(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        await self.bridge.after_ocpp_call("MeterValues")
        await self.bridge._initialization_task  # noqa: SLF001
        self.assertEqual(
            self.connection.calls,
            [
                ("TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 1}),
            ],
        )
        await self.bridge.after_ocpp_call("Heartbeat")
        self.assertEqual(len(self.connection.calls), 1)

    async def test_boot_does_not_reset_recovered_active_session(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        await self.bridge.handle_ocpp_call("MeterValues", {"connectorId": 1, "transactionId": 123})
        await self.bridge.handle_ocpp_call("BootNotification", {})
        self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_central_status_does_not_override_connector_session(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Charging"}
        )
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 0, "status": "Available"}
        )
        self.assertIsNone(self.mqtt.values["charge_control"])


class PowerExtractionTests(unittest.TestCase):
    def test_prefers_standard_power_measurand(self) -> None:
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {
                            "value": "1250",
                            "measurand": "Power.Active.Import",
                            "unit": "W",
                        }
                    ]
                }
            ]
        }
        self.assertAlmostEqual(extract_power_kw(payload, number_of_phases=3), 1.25)

    def test_falls_back_to_three_phase_current_and_voltage(self) -> None:
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"value": "10", "measurand": "Current.Import", "unit": "A"},
                        {"value": "230", "measurand": "Voltage", "unit": "V"},
                    ]
                }
            ]
        }
        self.assertAlmostEqual(extract_power_kw(payload, number_of_phases=3), 6.9)

    def test_uses_latest_meter_value_instead_of_summing_timestamps(self) -> None:
        payload = {
            "meterValue": [
                {
                    "timestamp": "2026-09-30T12:00:00Z",
                    "sampledValue": [
                        {"value": "1000", "measurand": "Power.Active.Import", "unit": "W"}
                    ],
                },
                {
                    "timestamp": "2026-09-30T12:01:00Z",
                    "sampledValue": [
                        {"value": "2000", "measurand": "Power.Active.Import", "unit": "W"}
                    ],
                },
            ]
        }
        self.assertAlmostEqual(extract_power_kw(payload, number_of_phases=3), 2.0)


class CurrentExtractionTests(unittest.TestCase):
    def test_prefers_unphased_current_import(self) -> None:
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"value": "11.2", "measurand": "Current.Import", "unit": "A"},
                        {
                            "value": "7",
                            "measurand": "Current.Import",
                            "phase": "L1",
                            "unit": "A",
                        },
                    ]
                }
            ]
        }
        self.assertAlmostEqual(extract_current_a(payload), 11.2)

    def test_averages_only_active_phase_currents(self) -> None:
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"value": "9", "measurand": "Current.Import", "phase": "L1"},
                        {"value": "10", "measurand": "Current.Import", "phase": "L2"},
                        {"value": "11", "measurand": "Current.Import", "phase": "L3"},
                    ]
                }
            ]
        }
        self.assertAlmostEqual(extract_current_a(payload), 10.0)

        payload["meterValue"][0]["sampledValue"][1]["value"] = "0"
        payload["meterValue"][0]["sampledValue"][2]["value"] = "0"
        self.assertAlmostEqual(extract_current_a(payload), 9.0)

    def test_all_zero_phases_publish_zero(self) -> None:
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"value": "0", "measurand": "Current.Import", "phase": "L1"},
                        {"value": "0", "measurand": "Current.Import", "phase": "L2"},
                        {"value": "0", "measurand": "Current.Import", "phase": "L3"},
                    ]
                }
            ]
        }
        self.assertEqual(extract_current_a(payload), 0.0)

    def test_uses_latest_meter_value_and_ignores_invalid_samples(self) -> None:
        payload = {
            "meterValue": [
                {"sampledValue": [{"value": "6", "measurand": "Current.Import"}]},
                {
                    "sampledValue": [
                        {"value": "nan", "measurand": "Current.Import"},
                        {"value": "7000", "measurand": "Current.Import", "unit": "mA"},
                    ]
                },
            ]
        }
        self.assertIsNone(extract_current_a(payload))


if __name__ == "__main__":
    unittest.main()
