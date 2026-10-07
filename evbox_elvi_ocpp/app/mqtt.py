"""MQTT connection and Home Assistant discovery publication."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Config

LOGGER = logging.getLogger(__name__)
CommandHandler = Callable[[str, str], Awaitable[None]]


class InvalidMqttCommand(ValueError):
    """An MQTT command whose topic or payload is invalid."""


class MqttCommandRejected(RuntimeError):
    """An otherwise valid MQTT command rejected by the charger."""


class MqttBridge:
    """Publish charger entities and forward MQTT commands to the bridge."""

    AVAILABILITY = "availability"
    CHARGE_CONTROL = "charge_control"
    MAXIMUM_CURRENT = "maximum_current"
    CURRENT_IMPORT = "current_import"
    POWER_ACTIVE_IMPORT = "power_active_import"
    CHARGER_AVAILABILITY = "charger_availability"
    DEFAULT_ENTITY_IDS = {
        CHARGE_CONTROL: "switch.charger_charge_control",
        CHARGER_AVAILABILITY: "switch.charger_availability",
        MAXIMUM_CURRENT: "number.charger_maximum_current",
        CURRENT_IMPORT: "sensor.charger_current_import",
        POWER_ACTIVE_IMPORT: "sensor.charger_power_active_import",
    }

    def __init__(self, config: Config) -> None:
        import paho.mqtt.client as mqtt

        self._config = config
        self._mqtt_api = mqtt
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()
        self._command_handler: CommandHandler | None = None
        self._manufacturer = "EV-BOX"
        self._model = "Elvi"
        self._serial_number: str | None = None
        self._firmware_version: str | None = None
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="evbox-elvi-ocpp-bridge",
            protocol=mqtt.MQTTv311,
        )
        if config.mqtt_username:
            self._client.username_pw_set(config.mqtt_username, config.mqtt_password)
        self._client.will_set(self.topic(self.AVAILABILITY), "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def topic(self, suffix: str) -> str:
        """Return one state or command topic below the app prefix."""
        return f"{self._config.mqtt_topic_prefix}/{suffix}"

    def set_command_handler(self, handler: CommandHandler) -> None:
        """Set the coroutine invoked for command messages."""
        self._command_handler = handler

    async def start(self) -> None:
        """Connect to the Supervisor-provided MQTT service."""
        self._loop = asyncio.get_running_loop()
        LOGGER.info(
            "Connecting to MQTT broker at %s:%s",
            self._config.mqtt_host,
            self._config.mqtt_port,
        )
        self._client.connect(self._config.mqtt_host, self._config.mqtt_port, keepalive=60)
        self._client.loop_start()
        await asyncio.wait_for(self._connected.wait(), timeout=30)

    async def stop(self) -> None:
        """Publish an orderly offline state and disconnect."""
        if self._client.is_connected():
            self.publish(self.AVAILABILITY, "offline")
            self._client.disconnect()
        self._client.loop_stop()

    def publish_discovery(self) -> None:
        """Publish retained MQTT Discovery definitions for the charger entities."""
        device = {
            "identifiers": ["evbox_elvi_ocpp_bridge"],
            "name": "charger",
            "manufacturer": self._manufacturer,
            "model": self._model,
        }
        if self._serial_number:
            device["serial_number"] = self._serial_number
        if self._firmware_version:
            device["sw_version"] = self._firmware_version

        common = {
            "availability_topic": self.topic(self.AVAILABILITY),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            "origin": {
                "name": "EVBox Elvi OCPP bridge",
                "sw_version": "1.2.0",
                "support_url": "https://github.com/davehen/ha-ocpp-server-app",
            },
            "qos": 1,
        }
        entities: tuple[tuple[str, str, dict[str, object]], ...] = (
            (
                "switch",
                self.CHARGE_CONTROL,
                {
                    "name": "Charge Control",
                    "unique_id": "evbox_elvi_charge_control",
                    "default_entity_id": self.DEFAULT_ENTITY_IDS[self.CHARGE_CONTROL],
                    "icon": "mdi:ev-station",
                    "command_topic": self.topic(f"{self.CHARGE_CONTROL}/set"),
                    "state_topic": self.topic(f"{self.CHARGE_CONTROL}/state"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
            ),
            (
                "switch",
                self.CHARGER_AVAILABILITY,
                {
                    "name": "Availability",
                    "unique_id": "evbox_elvi_availability",
                    "default_entity_id": self.DEFAULT_ENTITY_IDS[self.CHARGER_AVAILABILITY],
                    "icon": "mdi:ev-station",
                    "command_topic": self.topic(f"{self.CHARGER_AVAILABILITY}/set"),
                    "state_topic": self.topic(f"{self.CHARGER_AVAILABILITY}/state"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
            ),
            (
                "number",
                self.MAXIMUM_CURRENT,
                {
                    "name": "Maximum Current",
                    "unique_id": "evbox_elvi_maximum_current",
                    "default_entity_id": self.DEFAULT_ENTITY_IDS[self.MAXIMUM_CURRENT],
                    "icon": "mdi:ev-station",
                    "command_topic": self.topic(f"{self.MAXIMUM_CURRENT}/set"),
                    "state_topic": self.topic(f"{self.MAXIMUM_CURRENT}/state"),
                    "unit_of_measurement": "A",
                    "min": 0,
                    "max": self._config.maximum_current,
                    "step": 0.1,
                    "mode": "slider",
                },
            ),
            (
                "sensor",
                self.CURRENT_IMPORT,
                {
                    "name": "Current Import",
                    "unique_id": "evbox_elvi_current_import",
                    "default_entity_id": self.DEFAULT_ENTITY_IDS[self.CURRENT_IMPORT],
                    "icon": "mdi:current-ac",
                    "state_topic": self.topic(f"{self.CURRENT_IMPORT}/state"),
                    "unit_of_measurement": "A",
                    "device_class": "current",
                    "state_class": "measurement",
                    "suggested_display_precision": 3,
                },
            ),
            (
                "sensor",
                self.POWER_ACTIVE_IMPORT,
                {
                    "name": "Power Active Import",
                    "unique_id": "evbox_elvi_power_active_import",
                    "default_entity_id": self.DEFAULT_ENTITY_IDS[self.POWER_ACTIVE_IMPORT],
                    "icon": "mdi:ev-station",
                    "state_topic": self.topic(f"{self.POWER_ACTIVE_IMPORT}/state"),
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "state_class": "measurement",
                    "suggested_display_precision": 3,
                },
            ),
        )
        for component, object_name, entity in entities:
            payload = {**common, **entity}
            discovery_topic = f"homeassistant/{component}/evbox_elvi/{object_name}/config"
            self._publish_raw(
                discovery_topic,
                json.dumps(payload, separators=(",", ":")),
                retain=True,
            )

    def update_device_information(
        self,
        *,
        manufacturer: str | None,
        model: str | None,
        serial_number: str | None,
        firmware_version: str | None,
    ) -> None:
        """Update device metadata learned from BootNotification."""
        self._manufacturer = manufacturer or self._manufacturer
        self._model = model or self._model
        self._serial_number = serial_number or self._serial_number
        self._firmware_version = firmware_version or self._firmware_version
        self.publish_discovery()

    def publish(self, suffix: str, value: str | float | int, *, retain: bool = True) -> None:
        """Publish a value below the app topic prefix."""
        self._publish_raw(self.topic(suffix), str(value), retain=retain)

    def publish_charge_control(self, enabled: bool) -> None:
        """Publish the acknowledged charge-control state."""
        self.publish(f"{self.CHARGE_CONTROL}/state", "ON" if enabled else "OFF")

    def publish_charger_availability(self, enabled: bool) -> None:
        """Publish whether the connector is physically available."""
        self.publish(f"{self.CHARGER_AVAILABILITY}/state", "ON" if enabled else "OFF")

    def publish_maximum_current(self, amperes: float) -> None:
        """Publish the charger-acknowledged current limit."""
        self.publish(f"{self.MAXIMUM_CURRENT}/state", f"{amperes:g}")

    def publish_power(self, kilowatts: float) -> None:
        """Publish active imported power in kilowatts."""
        self.publish(f"{self.POWER_ACTIVE_IMPORT}/state", f"{max(0.0, kilowatts):.3f}")

    def publish_current(self, amperes: float) -> None:
        """Publish measured imported current in amperes."""
        self.publish(f"{self.CURRENT_IMPORT}/state", f"{max(0.0, amperes):.3f}")

    def publish_charger_online(self, online: bool) -> None:
        """Publish entity availability based on the OCPP connection."""
        self.publish(self.AVAILABILITY, "online" if online else "offline")

    def _publish_raw(self, topic: str, payload: str, *, retain: bool) -> None:
        result = self._client.publish(topic, payload, qos=1, retain=retain)
        if result.rc != self._mqtt_api.MQTT_ERR_SUCCESS:
            LOGGER.error("MQTT publish failed for %s with rc=%s", topic, result.rc)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code.is_failure:
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        LOGGER.info("Connected to MQTT broker")
        client.subscribe(
            [
                (self.topic(f"{self.CHARGE_CONTROL}/set"), 1),
                (self.topic(f"{self.CHARGER_AVAILABILITY}/set"), 1),
                (self.topic(f"{self.MAXIMUM_CURRENT}/set"), 1),
                ("homeassistant/status", 0),
            ]
        )
        self.publish_discovery()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.set)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code.is_failure:
            LOGGER.warning("Disconnected unexpectedly from MQTT broker: %s", reason_code)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        payload = message.payload.decode("utf-8", errors="replace").strip()
        if message.topic == "homeassistant/status":
            if payload.lower() == "online":
                self.publish_discovery()
            return
        if self._command_handler is None or self._loop is None:
            LOGGER.error("Ignoring MQTT command before command handler is ready")
            return
        future = asyncio.run_coroutine_threadsafe(
            self._command_handler(message.topic, payload),
            self._loop,
        )
        future.add_done_callback(self._log_command_failure)

    @staticmethod
    def _log_command_failure(future: Any) -> None:
        try:
            future.result()
        except (InvalidMqttCommand, MqttCommandRejected) as exc:
            LOGGER.warning("MQTT command rejected: %s", exc)
        except Exception:
            LOGGER.exception("MQTT command failed")
