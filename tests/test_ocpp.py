"""Wire-level tests use complete 1.6 messages, not invalid empty placeholders."""

from __future__ import annotations

import asyncio
import json
import unittest

from app.ocpp import OcppCallError, OcppConnection

TRIGGER = {"requestedMessage": "StatusNotification", "connectorId": 1}
ACCEPTED = {"status": "Accepted"}
STATUS = {"connectorId": 1, "status": "Finishing", "errorCode": "NoError"}
START = {
    "connectorId": 1,
    "idTag": "HomeAssistant",
    "meterStart": 0,
    "timestamp": "2026-10-08T19:45:01Z",
}


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.changed = asyncio.Event()
        self.received = asyncio.Queue()

    async def send(self, message):
        self.sent.append(message)
        self.changed.set()

    async def recv(self):
        message = await self.received.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def close(self, **kwargs):
        self.closed = True

    async def wait_sent(self, count=1):
        async with asyncio.timeout(2):
            while len(self.sent) < count:
                self.changed.clear()
                await self.changed.wait()
        return json.loads(self.sent[count - 1])


class OcppConnectionTests(unittest.IsolatedAsyncioTestCase):
    def make_connection(self, timeout=1, handler=None):
        websocket = FakeWebSocket()
        self.effects, self.after = [], []

        async def handle(action, payload):
            self.effects.append((action, payload))
            if handler:
                return await handler(action, payload)
            if action == "StartTransaction":
                return {"transactionId": 123, "idTagInfo": ACCEPTED}
            if action == "BootNotification":
                return {
                    "status": "Accepted",
                    "currentTime": "2026-10-08T19:45:01Z",
                    "interval": 300,
                }
            return {}

        async def after(action):
            self.assertEqual(json.loads(websocket.sent[-1])[0], 3)
            self.after.append(action)

        connection = OcppConnection("charger", websocket, handle, after, timeout)
        return connection, websocket

    async def reply(self, connection, request, payload=None):
        await connection.handle_message(json.dumps([3, request[1], payload or ACCEPTED]))

    async def test_library_serializes_and_correlates_complete_remote_start(self):
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(
            connection.call("RemoteStartTransaction", {"idTag": "HomeAssistant", "connectorId": 1})
        )
        request = await websocket.wait_sent()
        self.assertEqual(
            request[2:], ["RemoteStartTransaction", {"idTag": "HomeAssistant", "connectorId": 1}]
        )
        await self.reply(connection, request)
        self.assertEqual(await pending, ACCEPTED)

    async def test_duplicate_result_is_ignored_without_queue_growth(self):
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(connection.call("TriggerMessage", TRIGGER))
        request = await websocket.wait_sent()
        await self.reply(connection, request)
        await self.reply(connection, request)
        self.assertEqual(await pending, ACCEPTED)
        self.assertTrue(connection._response_queue.empty())

    async def test_callerror_not_supported_is_not_suppressed(self):
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(
            connection.call("GetConfiguration", {"key": ["MeterValueSampleInterval"]})
        )
        request = await websocket.wait_sent()
        response = json.dumps([4, request[1], "NotSupported", "not supported", {}])
        await connection.handle_message(response)
        await connection.handle_message(response)
        with self.assertRaises(OcppCallError) as error:
            await pending
        self.assertEqual(error.exception.code, "NotSupported")
        self.assertTrue(connection._response_queue.empty())

    async def test_outbound_calls_are_serialized(self):
        connection, websocket = self.make_connection()
        first = asyncio.create_task(connection.call("TriggerMessage", TRIGGER))
        second = asyncio.create_task(
            connection.call("ChangeAvailability", {"connectorId": 1, "type": "Operative"})
        )
        request = await websocket.wait_sent()
        self.assertEqual(len(websocket.sent), 1)
        await self.reply(connection, request)
        await first
        request = await websocket.wait_sent(2)
        await self.reply(connection, request)
        await second

    async def test_timeout_late_reply_then_new_call(self):
        connection, websocket = self.make_connection(timeout=0.1)
        with self.assertRaises(TimeoutError):
            await connection.call("TriggerMessage", TRIGGER)
        old = await websocket.wait_sent()
        await self.reply(connection, old)
        pending = asyncio.create_task(connection.call("TriggerMessage", TRIGGER))
        request = await websocket.wait_sent(2)
        await self.reply(connection, request)
        self.assertEqual(await pending, ACCEPTED)

    async def test_timeout_also_bounds_blocked_send(self):
        connection, websocket = self.make_connection(timeout=0.1)

        async def blocked(message):
            await asyncio.Event().wait()

        websocket.send = blocked
        with self.assertRaises(TimeoutError):
            await connection.call("TriggerMessage", TRIGGER)
        self.assertIsNone(connection._expected_id)
        self.assertEqual(connection._call_tasks, set())

    async def test_disconnect_fails_pending_call_without_waiting_timeout(self):
        connection, websocket = self.make_connection(timeout=20)
        pending = asyncio.create_task(connection.call("TriggerMessage", TRIGGER))
        await websocket.wait_sent()
        await connection.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(pending, 1)

    async def test_external_cancellation_is_preserved_not_reported_as_disconnect(self):
        connection, websocket = self.make_connection()
        pending = asyncio.create_task(connection.call("TriggerMessage", TRIGGER))
        await websocket.wait_sent()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(connection.closed)

    async def test_duplicate_start_replays_response_without_second_allocation(self):
        connection, websocket = self.make_connection()
        incoming = json.dumps([2, "start-1", "StartTransaction", START])
        await connection.handle_message(incoming)
        await connection.handle_message(incoming)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(websocket.sent[0], websocket.sent[1])
        self.assertEqual(self.after, ["StartTransaction"])

    async def test_reused_id_with_different_payload_is_protocol_error(self):
        connection, websocket = self.make_connection()
        await connection.handle_message(json.dumps([2, "status-1", "StatusNotification", STATUS]))
        await connection.handle_message(
            json.dumps([2, "status-1", "StatusNotification", {**STATUS, "status": "Available"}])
        )
        self.assertEqual(json.loads(websocket.sent[-1])[0:3], [4, "status-1", "ProtocolError"])
        self.assertEqual(len(self.effects), 1)

    async def test_invalid_json_binary_ids_and_actions_do_not_kill_next_frame(self):
        connection, websocket = self.make_connection()
        for invalid in (
            "{",
            b"\xff",
            "[]",
            "{}",
            '[2,{},"StatusNotification",{}]',
            '[2,"id",{},{}]',
        ):
            await connection.handle_message(invalid)
        await connection.handle_message(json.dumps([2, "status-1", "StatusNotification", STATUS]))
        self.assertEqual(json.loads(websocket.sent[-1]), [3, "status-1", {}])

    async def test_schema_invalid_inbound_is_rejected_before_reducer(self):
        connection, websocket = self.make_connection()
        with self.assertLogs("app.ocpp.wire", "ERROR"):
            await connection.handle_message(
                json.dumps([2, "start-1", "StartTransaction", {"connectorId": 1}])
            )
        self.assertEqual(json.loads(websocket.sent[-1])[0], 4)
        self.assertEqual(self.effects, [])

    async def test_schema_invalid_outbound_is_not_sent(self):
        connection, websocket = self.make_connection()
        with self.assertRaises(OcppCallError):
            await connection.call("ChangeAvailability", {"connectorId": 1, "type": "invalid"})
        self.assertEqual(websocket.sent, [])

    async def test_valid_boot_has_complete_response_and_post_response_hook(self):
        connection, websocket = self.make_connection()
        await connection.handle_message(
            json.dumps(
                [
                    2,
                    "boot-1",
                    "BootNotification",
                    {"chargePointVendor": "EV-BOX", "chargePointModel": "Elvi"},
                ]
            )
        )
        result = json.loads(websocket.sent[-1])
        self.assertEqual(result[0], 3)
        self.assertEqual(result[2]["interval"], 300)
        self.assertEqual(self.after, ["BootNotification"])

    async def test_valid_but_unimplemented_action_has_library_callerror(self):
        connection, websocket = self.make_connection()
        with self.assertLogs("app.ocpp.wire", "ERROR"):
            await connection.handle_message(
                json.dumps(
                    [
                        2,
                        "unsupported-1",
                        "SecurityEventNotification",
                        {"type": "test", "timestamp": "2026-10-08T19:45:01Z"},
                    ]
                )
            )
        self.assertEqual(json.loads(websocket.sent[-1])[0], 4)

    async def test_replay_cache_is_bounded(self):
        connection, websocket = self.make_connection()
        for index in range(40):
            await connection.handle_message(
                json.dumps([2, f"status-{index}", "StatusNotification", STATUS])
            )
        self.assertEqual(len(connection._responses), 32)
