import unittest

from app.mqtt import MqttBridge


class MqttDiscoveryTests(unittest.TestCase):
    def test_preserves_required_home_assistant_entity_ids(self) -> None:
        self.assertEqual(
            MqttBridge.DEFAULT_ENTITY_IDS,
            {
                "charge_control": "switch.charger_charge_control",
                "charger_availability": "switch.charger_availability",
                "maximum_current": "number.charger_maximum_current",
                "power_active_import": "sensor.charger_power_active_import",
            },
        )


if __name__ == "__main__":
    unittest.main()
