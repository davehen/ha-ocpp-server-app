import os
import unittest
from unittest import mock

from app.config import Config


class ConfigTests(unittest.TestCase):
    def test_rejects_nonfinite_or_out_of_range_configuration(self) -> None:
        for name, value in (
            ("MAXIMUM_CURRENT", "nan"),
            ("MAXIMUM_CURRENT", "inf"),
            ("MAXIMUM_CURRENT", "33"),
            ("COMMAND_TIMEOUT", "0"),
            ("OCPP_PORT", "65536"),
            ("LOG_LEVEL", "INVALID"),
        ):
            with self.subTest(name=name, value=value):
                with mock.patch.dict(os.environ, {"MQTT_HOST": "mqtt", name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        Config.from_environment()

    def test_rejects_oversized_ocpp_id_tag(self) -> None:
        environment = {
            "MQTT_HOST": "mqtt",
            "OCPP_ID_TAG": "x" * 21,
            "DATA_DIRECTORY": "/data",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "id_tag"):
                Config.from_environment()


if __name__ == "__main__":
    unittest.main()
