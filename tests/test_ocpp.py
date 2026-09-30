from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.ocpp import OcppConnection


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class OcppConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_charge_point_call_is_correlated_with_result(self) -> None:
        websocket = FakeWebSocket()

        async def handle_call(action: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def after_call(action: str) -> None:
            return None

        connection = OcppConnection(
            "charger",
            websocket,  # type: ignore[arg-type]
            handle_call,
            after_call,
            command_timeout=1,
        )
        pending = asyncio.create_task(
            connection.call("RemoteStartTransaction", {"idTag": "HA"})
        )
        await asyncio.sleep(0)
        request = json.loads(websocket.sent[-1])
        self.assertEqual(request[0], 2)
        self.assertEqual(request[2], "RemoteStartTransaction")

        await connection.handle_message(json.dumps([3, request[1], {"status": "Accepted"}]))
        self.assertEqual(await pending, {"status": "Accepted"})

    async def test_incoming_boot_notification_gets_call_result(self) -> None:
        websocket = FakeWebSocket()
        after_actions: list[str] = []

        async def handle_call(action: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(action, "BootNotification")
            return {"status": "Accepted"}

        async def after_call(action: str) -> None:
            after_actions.append(action)

        connection = OcppConnection(
            "charger",
            websocket,  # type: ignore[arg-type]
            handle_call,
            after_call,
            command_timeout=1,
        )
        await connection.handle_message(json.dumps([2, "request-1", "BootNotification", {}]))

        self.assertEqual(
            json.loads(websocket.sent[-1]),
            [3, "request-1", {"status": "Accepted"}],
        )
        self.assertEqual(after_actions, ["BootNotification"])


if __name__ == "__main__":
    unittest.main()
