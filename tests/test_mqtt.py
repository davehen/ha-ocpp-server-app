import json
import unittest
from types import SimpleNamespace

from app.mqtt import MqttBridge


class MqttDiscoveryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
