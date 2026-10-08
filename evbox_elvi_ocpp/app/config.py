"""Runtime configuration loaded from the Home Assistant app environment."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Configuration for the OCPP and MQTT bridge."""

    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    expected_charge_point_id: str
    id_tag: str
    heartbeat_interval: int
    meter_value_interval: int
    configure_meter_values: bool
    maximum_current: float
    number_of_phases: int
    command_timeout: int
    log_level: str
    data_directory: Path
    ocpp_port: int = 9000
    mqtt_topic_prefix: str = "evbox_elvi"

    @classmethod
    def from_environment(cls) -> Config:
        """Load and validate configuration from environment variables."""
        config = cls(
            mqtt_host=os.environ["MQTT_HOST"],
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
            mqtt_username=os.getenv("MQTT_USERNAME", ""),
            mqtt_password=os.getenv("MQTT_PASSWORD", ""),
            expected_charge_point_id=os.getenv("EXPECTED_CHARGE_POINT_ID", "").strip(),
            id_tag=os.getenv("OCPP_ID_TAG", "HomeAssistant").strip(),
            heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "300")),
            meter_value_interval=int(os.getenv("METER_VALUE_INTERVAL", "60")),
            configure_meter_values=_get_bool("CONFIGURE_METER_VALUES", True),
            maximum_current=float(os.getenv("MAXIMUM_CURRENT", "16")),
            number_of_phases=int(os.getenv("NUMBER_OF_PHASES", "3")),
            command_timeout=int(os.getenv("COMMAND_TIMEOUT", "20")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            data_directory=Path(os.getenv("DATA_DIRECTORY", "/data")),
            ocpp_port=int(os.getenv("OCPP_PORT", "9000")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Raise ValueError when a value cannot be used safely."""
        if not self.mqtt_host:
            raise ValueError("MQTT_HOST must not be empty")
        if not self.id_tag or len(self.id_tag) > 20:
            raise ValueError("OCPP id_tag must contain between 1 and 20 characters")
        if self.number_of_phases not in {1, 3}:
            raise ValueError("number_of_phases must be 1 or 3")
        if not math.isfinite(self.maximum_current) or not 6 <= self.maximum_current <= 32:
            raise ValueError("maximum_current must be finite and between 6 and 32 A")
        for name, value, minimum, maximum in (
            ("mqtt_port", self.mqtt_port, 1, 65535),
            ("ocpp_port", self.ocpp_port, 1, 65535),
            ("heartbeat_interval", self.heartbeat_interval, 30, 3600),
            ("meter_value_interval", self.meter_value_interval, 10, 3600),
            ("command_timeout", self.command_timeout, 5, 60),
        ):
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Invalid log_level")
