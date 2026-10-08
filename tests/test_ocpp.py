from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.ocpp import OcppCallError, OcppConnection


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class OcppConnectionTests(unittest.IsolatedAsyncioTestCase):
    def make_connection(self, websocket=None, timeout=1):
        websocket = websocket or FakeWebSocket()

        async def handle(action, payload):
            return {}

        async def after(action):
            pass

        return OcppConnection("charger", websocket, handle, after, timeout), websocket

    async def test_duplicate_result_before_cleanup_is_ignored(self) -> None:
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(connection.call("TriggerMessage", {}))
        await asyncio.sleep(0)
        unique_id = json.loads(websocket.sent[-1])[1]
        response = json.dumps([3, unique_id, {"status": "Accepted"}])
        await connection.handle_message(response)
        await connection.handle_message(response)
        self.assertEqual(await pending, {"status": "Accepted"})

    async def test_duplicate_error_before_cleanup_is_ignored(self) -> None:
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(connection.call("TriggerMessage", {}))
        await asyncio.sleep(0)
        unique_id = json.loads(websocket.sent[-1])[1]
        response = json.dumps([4, unique_id, "NotSupported", "not supported", {}])
        await connection.handle_message(response)
        await connection.handle_message(response)
        with self.assertRaises(OcppCallError):
            await pending

    async def test_outbound_calls_wait_for_previous_response(self) -> None:
        connection, websocket = self.make_connection()
        first = asyncio.create_task(connection.call("TriggerMessage", {}))
        second = asyncio.create_task(connection.call("SetChargingProfile", {}))
        await asyncio.sleep(0)
        self.assertEqual(len(websocket.sent), 1)
        await connection.handle_message(json.dumps([3, json.loads(websocket.sent[0])[1], {}]))
        await first
        await asyncio.sleep(0)
        self.assertEqual(len(websocket.sent), 2)
        await connection.handle_message(json.dumps([3, json.loads(websocket.sent[1])[1], {}]))
        await second

    async def test_timeout_and_late_response_do_not_break_next_call(self) -> None:
        connection, websocket = self.make_connection(timeout=0.01)
        with self.assertRaises(TimeoutError):
            await connection.call("TriggerMessage", {})
        old_id = json.loads(websocket.sent[0])[1]
        await connection.handle_message(json.dumps([3, old_id, {}]))
        pending = asyncio.create_task(connection.call("TriggerMessage", {}))
        await asyncio.sleep(0)
        await connection.handle_message(json.dumps([3, json.loads(websocket.sent[-1])[1], {}]))
        await pending

    async def test_timeout_also_bounds_a_blocked_send(self) -> None:
        websocket = FakeWebSocket()

        async def blocked_send(message):
            await asyncio.Event().wait()

        websocket.send = blocked_send
        connection, _ = self.make_connection(websocket, timeout=0.01)
        with self.assertRaises(TimeoutError):
            await connection.call("TriggerMessage", {})
        self.assertEqual(connection._pending, {})

    async def test_duplicate_incoming_call_reuses_response_without_side_effects(self) -> None:
        connection, websocket = self.make_connection()
        count = 0

        async def handle(action, payload):
            nonlocal count
            count += 1
            return {"transactionId": count}

        connection._call_handler = handle
        call = json.dumps([2, "start-1", "StartTransaction", {"connectorId": 1}])
        await connection.handle_message(call)
        await connection.handle_message(call)
        self.assertEqual(count, 1)
        self.assertEqual(websocket.sent[0], websocket.sent[1])

    async def test_invalid_json_and_binary_do_not_break_subsequent_frames(self) -> None:
        connection, websocket = self.make_connection()
        await connection.handle_message("{")
        await connection.handle_message(b"\xff")
        await connection.handle_message(json.dumps([2, "status-1", "StatusNotification", {}]))
        self.assertEqual(json.loads(websocket.sent[-1]), [3, "status-1", {}])

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
        pending = asyncio.create_task(connection.call("RemoteStartTransaction", {"idTag": "HA"}))
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
