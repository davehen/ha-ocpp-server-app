from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from app.bridge import BridgeController, extract_power_kw
from app.config import Config
from app.mqtt import MqttBridge
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

    def publish_charge_control(self, enabled: bool) -> None:
        self.values["charge_control"] = enabled

    def publish_charger_availability(self, enabled: bool) -> None:
        self.values["availability"] = enabled

    def publish_power(self, kilowatts: float) -> None:
        self.values["power"] = kilowatts

    def publish_maximum_current(self, amperes: float) -> None:
        self.values["maximum_current"] = amperes

    def update_device_information(self, **values: Any) -> None:
        self.device_information = values


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, payload))
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
        self.assertFalse(self.mqtt.values["charge_control"])

        await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "status": "Charging", "errorCode": "NoError"},
        )
        self.assertTrue(self.mqtt.values["charge_control"])

        self.mqtt.values["power"] = 2.5
        await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "status": "SuspendedEVSE", "errorCode": "NoError"},
        )
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["power"], 0)

    async def test_current_command_uses_elvi_tx_default_profile(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]

        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8.5")

        action, payload = self.connection.calls[-1]
        self.assertEqual(action, "SetChargingProfile")
        self.assertEqual(payload["connectorId"], 1)
        profile = payload["csChargingProfiles"]
        self.assertEqual(profile["chargingProfilePurpose"], "TxDefaultProfile")
        self.assertEqual(profile["chargingProfileKind"], "Relative")
        self.assertEqual(
            profile["chargingSchedule"]["chargingSchedulePeriod"],
            [{"startPeriod": 0, "limit": 8.5}],
        )
        self.assertEqual(self.mqtt.values["maximum_current"], 8.5)

    async def test_five_amp_pause_value_is_forwarded_unchanged(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]

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
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}

        with self.assertRaisesRegex(RuntimeError, "rejected SetChargingProfile"):
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "7")

        self.assertNotIn("maximum_current", self.mqtt.values)

    async def test_remote_start_and_stop_use_wallbox_transaction(self) -> None:
        await self.bridge.attach(self.connection)  # type: ignore[arg-type]

        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        self.assertEqual(
            self.connection.calls[-1],
            ("RemoteStartTransaction", {"idTag": "HomeAssistant", "connectorId": 1}),
        )
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
        self.assertFalse(self.mqtt.values["charge_control"])


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


if __name__ == "__main__":
    unittest.main()
