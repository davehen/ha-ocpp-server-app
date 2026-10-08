"""Application entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from websockets.legacy.server import WebSocketServerProtocol

from .bridge import BridgeController
from .config import Config
from .mqtt import MqttBridge
from .ocpp import OcppConnection
from .state import StateStore

LOGGER = logging.getLogger(__name__)


async def run() -> None:
    """Run MQTT and the OCPP WebSocket server until terminated."""
    from websockets.legacy.server import serve

    config = Config.from_environment()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    mqtt_bridge = MqttBridge(config)
    state_store = StateStore(config.data_directory, config.maximum_current)
    controller = BridgeController(config, mqtt_bridge, state_store)
    await mqtt_bridge.start()
    controller.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_name, stop_event.set)

    async def handle_websocket(
        websocket: WebSocketServerProtocol,
        request_path: str,
    ) -> None:
        if websocket.subprotocol != "ocpp1.6":
            LOGGER.warning("Rejecting connection without the ocpp1.6 subprotocol")
            await websocket.close(code=1002, reason="OCPP 1.6J subprotocol required")
            return
        charge_point_id = charge_point_id_from_path(request_path)
        if not charge_point_id:
            await websocket.close(code=1008, reason="Charge point ID missing from URL")
            return
        expected = config.expected_charge_point_id
        if expected and charge_point_id != expected:
            LOGGER.warning("Rejecting unexpected charge point ID %s", charge_point_id)
            await websocket.close(code=1008, reason="Unexpected charge point ID")
            return

        LOGGER.info("EVBox connected with charge point ID %s", charge_point_id)
        connection = OcppConnection(
            charge_point_id,
            websocket,
            lambda action, payload: controller.handle_ocpp_call(
                action, payload, connection=connection
            ),
            lambda action: controller.after_ocpp_call(action, connection=connection),
            config.command_timeout,
        )
        try:
            await controller.attach(connection)
            await connection.run()
        finally:
            controller.detach(connection)

    try:
        async with serve(
            handle_websocket,
            "0.0.0.0",
            config.ocpp_port,
            subprotocols=["ocpp1.6"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_size=1_048_576,
        ):
            LOGGER.info("OCPP 1.6J server listening on port %s", config.ocpp_port)
            await stop_event.wait()
    finally:
        await mqtt_bridge.stop()


def charge_point_id_from_path(request_path: str) -> str:
    """Extract the final path segment from origin-form or absolute-form requests."""
    parsed = urlparse(request_path)
    path = parsed.path if parsed.scheme else request_path.split("?", maxsplit=1)[0]
    segments = [unquote(segment) for segment in path.split("/") if segment]
    return segments[-1] if segments else ""


def main() -> None:
    """Start the asyncio application."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
