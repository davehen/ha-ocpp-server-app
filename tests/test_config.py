import os
import unittest
from unittest import mock

from app.config import Config


class ConfigTests(unittest.TestCase):
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
