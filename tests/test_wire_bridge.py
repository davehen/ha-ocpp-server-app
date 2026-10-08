"""Real loopback WebSockets exercise the library, controller and Elvi mock.

MQTT publication is captured in memory; verify.sh separately covers a real
Mosquitto broker and the real Python 3.13 add-on image.
"""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path

import test_bridge as fixtures
from app.bridge import BridgeController
from app.ocpp import OcppConnection
from websockets.legacy.client import connect
from websockets.legacy.server import serve

mock_path = Path(__file__).resolve().parents[1] / "dev_scripts" / "smoke_client.py"
spec = importlib.util.spec_from_file_location("elvi_mock", mock_path)
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class WireBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mqtt = fixtures.FakeMqtt()
        self.changed = asyncio.Event()
        original_publish = self.mqtt._publish

        def publish(name, value):
            original_publish(name, value)
            self.changed.set()

        self.mqtt._publish = publish
        self.store = fixtures.FakeStateStore(8)
        self.config = replace(fixtures.config(), configure_meter_values=True)
        self.bridge = BridgeController(self.config, self.mqtt, self.store)
        self.bridge.start()

        async def accept(websocket, path):
            connection = OcppConnection(
                "EVB-P123",
                websocket,
                lambda action, payload: self.bridge.handle_ocpp_call(
                    action, payload, connection=connection
                ),
                lambda action: self.bridge.after_ocpp_call(action, connection=connection),
                2,
            )
            try:
                await self.bridge.attach(connection)
                await connection.run()
            finally:
                self.bridge.detach(connection)

        self.server = await serve(accept, "127.0.0.1", 0, subprotocols=["ocpp1.6"])
        self.uri = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/EVB-P123"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def wait(self, predicate):
        async with asyncio.timeout(3):
            while True:
                self.changed.clear()
                if predicate():
                    return
                await self.changed.wait()

    async def command(self, entity, value):
        await self.bridge.handle_mqtt_command(f"evbox_elvi/{entity}/set", str(value))

    async def test_finishing_unsupported_setup_profiled_start_dynamic_pause_resume_and_stop(self):
        async with connect(self.uri, subprotocols=["ocpp1.6"]) as websocket:
            wallbox = mock.MockWallbox(websocket, self.changed)
            try:
                await wallbox.call(
                    "BootNotification", {"chargePointVendor": "EV-BOX", "chargePointModel": "Elvi"}
                )
                await self.wait(lambda: self.mqtt.values.get("maximum_current") == 8)
                await self.wait(lambda: any(a == "GetConfiguration" for a, _ in wallbox.commands))
                self.assertEqual(self.bridge.state.status, "Finishing")
                await self.command("charge_control", "ON")
                self.assertFalse(self.mqtt.values["charge_control"])
                await wallbox.call(
                    "StartTransaction",
                    {
                        "connectorId": 1,
                        "idTag": "HomeAssistant",
                        "meterStart": 0,
                        "timestamp": fixtures.NOW,
                    },
                )
                transaction_id = wallbox.transaction_id
                await self.wait(lambda: wallbox.current == 8)
                await wallbox.call("MeterValues", wallbox.meter())
                for limit in (6, 5, 8, 8):
                    await self.command("maximum_current", limit)
                    await wallbox.notify_status()
                    await wallbox.call("MeterValues", wallbox.meter())
                    self.assertEqual(self.mqtt.values["current"], 0 if limit == 5 else limit)
                    self.assertTrue(self.mqtt.values["charge_control"])
                    self.assertEqual(self.bridge.state.transaction_id, transaction_id)
                await self.command("charge_control", "OFF")
                self.assertTrue(self.mqtt.values["charge_control"])
                await wallbox.call(
                    "StopTransaction",
                    {"transactionId": transaction_id, "meterStop": 0, "timestamp": fixtures.NOW},
                )
                self.assertFalse(self.mqtt.values["charge_control"])
                wallbox.transaction_id, wallbox.status, wallbox.current = None, "Finishing", 0
                await wallbox.notify_status()
                await self.wait(lambda: self.bridge.state.status == "Finishing")
                await self.command("charge_control", "ON")
                self.assertEqual(
                    [a for a, _ in wallbox.commands].count("RemoteStartTransaction"), 2
                )
                self.assertFalse(any(a == "Reset" for a, _ in wallbox.commands))
            finally:
                await wallbox.close()

    async def test_reconnect_without_boot_recovers_active_and_suspended_transaction(self):
        for current, limit in ((8, 8), (0, 5)):
            self.store.state.maximum_current = limit
            async with connect(self.uri, subprotocols=["ocpp1.6"]) as websocket:
                wallbox = mock.MockWallbox(websocket, self.changed, 123)
                wallbox.current = current
                wallbox.status = "Charging" if current else "SuspendedEVSE"
                try:
                    await wallbox.call("MeterValues", wallbox.meter())
                    await self.wait(
                        lambda limit=limit: self.mqtt.values.get("maximum_current") == limit
                    )
                    self.assertTrue(self.mqtt.values["charge_control"])
                    self.assertEqual(self.bridge.state.transaction_id, 123)
                    self.assertFalse(
                        any(
                            a in {"RemoteStartTransaction", "RemoteStopTransaction", "Reset"}
                            for a, _ in wallbox.commands
                        )
                    )
                finally:
                    await wallbox.close()
            await self.wait(lambda: self.mqtt.values.get("online") is False)
