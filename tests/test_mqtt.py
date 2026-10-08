import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace

from app.mqtt import InvalidMqttCommand, MqttBridge, MqttCommandRejected


class MqttDiscoveryTests(unittest.TestCase):
    def test_missing_measurements_publish_unknown_not_zero(self) -> None:
        bridge = MqttBridge.__new__(MqttBridge)
        published = []
        bridge.publish = lambda topic, payload: published.append(payload)
        bridge.publish_power(None)
        bridge.publish_current(None)
        bridge.publish_power(0)
        bridge.publish_current(8)
        self.assertEqual(published, ["None", "None", "0.000", "8.000"])

    def test_switch_unknown_and_known_payloads(self) -> None:
        bridge = MqttBridge.__new__(MqttBridge)
        published = []
        bridge.publish = lambda topic, payload: published.append((topic, payload))
        for method in (bridge.publish_charge_control, bridge.publish_charger_availability):
            for value in (None, True, False):
                method(value)
        self.assertEqual([payload for _, payload in published], ["None", "ON", "OFF"] * 2)

    def test_preserves_required_home_assistant_entity_ids(self) -> None:
        self.assertEqual(
            MqttBridge.DEFAULT_ENTITY_IDS,
            {
                "charge_control": "switch.charger_charge_control",
                "charger_availability": "switch.charger_availability",
                "maximum_current": "number.charger_maximum_current",
                "current_import": "sensor.charger_current_import",
                "power_active_import": "sensor.charger_power_active_import",
            },
        )

    def test_measured_current_discovery_metadata(self) -> None:
        bridge = MqttBridge.__new__(MqttBridge)
        bridge._config = SimpleNamespace(mqtt_topic_prefix="evbox_elvi", maximum_current=16)
        bridge._manufacturer = "EV-BOX"
        bridge._model = "Elvi"
        bridge._serial_number = None
        bridge._firmware_version = None
        published: dict[str, str] = {}
        bridge._publish_raw = lambda topic, payload, retain: published.__setitem__(topic, payload)

        bridge.publish_discovery()

        topic = "homeassistant/sensor/evbox_elvi/current_import/config"
        payload = json.loads(published[topic])
        self.assertEqual(payload["default_entity_id"], "sensor.charger_current_import")
        self.assertEqual(payload["state_topic"], "evbox_elvi/current_import/state")
        self.assertEqual(payload["unit_of_measurement"], "A")
        self.assertEqual(payload["device_class"], "current")
        self.assertEqual(payload["state_class"], "measurement")


class MqttCommandLoggingTests(unittest.TestCase):
    def test_expected_command_error_is_a_warning_without_traceback(self) -> None:
        for error in (
            InvalidMqttCommand("invalid payload"),
            MqttCommandRejected("charger rejected command"),
        ):
            with self.subTest(error=type(error).__name__):
                future: Future[None] = Future()
                future.set_exception(error)

                with self.assertLogs("app.mqtt", level="WARNING") as logs:
                    MqttBridge._log_command_failure(future)

                self.assertEqual(logs.records[0].levelname, "WARNING")
                self.assertIsNone(logs.records[0].exc_info)

    def test_unexpected_command_error_keeps_error_traceback(self) -> None:
        future: Future[None] = Future()
        future.set_exception(OSError("connection lost"))

        with self.assertLogs("app.mqtt", level="ERROR") as logs:
            MqttBridge._log_command_failure(future)

        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIsNotNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
