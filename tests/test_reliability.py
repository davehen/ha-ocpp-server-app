"""Race/recovery regressions, persisted-state integrity and MQTT restoration."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_bridge as fixtures
from app.bridge import BridgeController
from app.mqtt import MqttBridge, MqttCommandRejected
from app.state import StateStore


class PendingConnection(fixtures.FakeConnection):
    def __init__(self):
        super().__init__()
        self.sent, self.release = asyncio.Event(), asyncio.Event()

    async def call(self, action, payload):
        self.calls.append((action, payload))
        self.sent.set()
        await self.release.wait()
        return {"status": "Accepted"}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BridgeControllerTests.setUp
    asyncTearDown = fixtures.BridgeControllerTests.asyncTearDown
    command = fixtures.BridgeControllerTests.command
    status = fixtures.BridgeControllerTests.status
    drain = fixtures.BridgeControllerTests.drain
    active = fixtures.BridgeControllerTests.active

    async def test_restart_recovers_suspended_session_from_zero_meter_without_boot(self):
        self.store.state.maximum_current = 5
        await self.active(current="0", power="0")
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["current"], 0)
        self.assertIsNone(self.mqtt.values["maximum_current"])
        await self.drain()
        self.assertEqual(self.mqtt.values["maximum_current"], 5)
        self.assertFalse(
            any(
                a in {"RemoteStartTransaction", "RemoteStopTransaction", "Reset"}
                for a, _ in self.connection.calls
            )
        )

    async def test_restart_recovers_active_session_and_limit_from_same_store(self):
        await self.active()
        await self.command("maximum_current", 6)
        self.bridge.detach(self.connection)
        self.bridge = BridgeController(self.config, self.mqtt, self.store)
        self.connection = fixtures.FakeConnection()
        await self.active()
        await self.drain()
        self.assertEqual(self.mqtt.values["maximum_current"], 6)
        self.assertEqual(self.bridge.state.transaction_id, 123)

    async def test_idle_clock_energy_does_not_invent_session_or_zero_power(self):
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "MeterValues",
            {
                "connectorId": 1,
                "meterValue": [
                    {
                        "timestamp": "2026-10-08T19:30:00Z",
                        "sampledValue": [
                            {
                                "value": "24930370",
                                "context": "Sample.Clock",
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertIsNone(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["power"])
        await self.status("Finishing", timestamp=fixtures.NOW)
        await self.drain()
        self.assertEqual(self.mqtt.values["maximum_current"], 8)
        await self.command("charge_control", "ON")
        self.assertEqual(self.connection.calls[-1][0], "RemoteStartTransaction")

    async def test_rejected_recovery_is_one_bounded_attempt_not_a_meter_retry_storm(self):
        await self.active()
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}
        await self.drain()
        for _ in range(5):
            await self.drain("Heartbeat")
        profiles = [a for a, _ in self.connection.calls if a == "SetChargingProfile"]
        self.assertEqual(len(profiles), 1)
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["maximum_current"])
        self.connection.responses.clear()
        await self.command("maximum_current", 8)
        self.assertEqual(self.mqtt.values["maximum_current"], 8)

    async def test_queued_command_cannot_cross_connection_or_session(self):
        await self.active()
        await self.bridge._command_lock.acquire()
        pending = asyncio.create_task(self.command("maximum_current", 5))
        await asyncio.sleep(0)
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        self.bridge._command_lock.release()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertEqual(self.connection.calls, [])

    async def test_old_profile_ack_cannot_commit_to_replacement_connection(self):
        self.connection = PendingConnection()
        await self.active()
        old = self.connection
        pending = asyncio.create_task(self.command("maximum_current", 5))
        await old.sent.wait()
        await self.bridge.attach(fixtures.FakeConnection())
        old.release.set()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertEqual(self.store.state.maximum_current, 8)
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_boot_on_same_socket_retires_old_profile_ack(self):
        self.connection = PendingConnection()
        await self.active()
        pending = asyncio.create_task(self.command("maximum_current", 5))
        await self.connection.sent.wait()
        await self.bridge.handle_ocpp_call("BootNotification", {})
        self.connection.release.set()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertEqual(self.store.state.maximum_current, 8)

    async def test_retired_socket_cannot_send_stop_for_new_session(self):
        await self.active()
        old = self.connection
        await self.bridge.attach(fixtures.FakeConnection())
        await self.bridge.handle_ocpp_call("MeterValues", fixtures.meter(222))
        with self.assertRaises(ConnectionError):
            await self.bridge.handle_ocpp_call(
                "StopTransaction", {"transactionId": 222}, connection=old
            )
        self.assertEqual(self.bridge.state.transaction_id, 222)

    async def test_closed_transaction_cannot_be_resurrected_after_reconnect(self):
        await self.active()
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        await self.bridge.attach(fixtures.FakeConnection())
        await self.bridge.handle_ocpp_call("MeterValues", fixtures.meter(123))
        self.assertIsNone(self.bridge.state.transaction_id)
        self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_old_stop_and_other_transaction_meter_do_not_override_live_session(self):
        await self.active(222)
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 111})
        await self.bridge.handle_ocpp_call("MeterValues", fixtures.meter(111, "0", "0"))
        self.assertEqual(self.bridge.state.transaction_id, 222)
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["power"], 5.52)

    async def test_foreign_stop_does_not_poison_current_transaction_watermark(self):
        await self.active(222)
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 999})
        await self.bridge.handle_ocpp_call("MeterValues", fixtures.meter(222, "6", "4140"))
        self.assertEqual(self.mqtt.values["current"], 6)
        self.assertLess(self.store.state.last_closed_transaction_id, 222)

    async def test_preparing_on_reconnect_does_not_block_transaction_evidence(self):
        await self.bridge.attach(self.connection)
        await self.status("Preparing")
        await self.bridge.handle_ocpp_call("MeterValues", fixtures.meter(123, "0", "0"))
        self.assertEqual(self.bridge.state.transaction_id, 123)
        self.assertTrue(self.mqtt.values["charge_control"])

    async def test_failed_active_default_is_restored_to_target_after_stop(self):
        await self.active()
        self.bridge.state.default_limit = 12
        self.connection.response_sequences["SetChargingProfile"] = [
            {"status": "Accepted"},
            {"status": "Rejected"},
        ]
        await self.command("maximum_current", 6)
        self.assertEqual(self.bridge.state.default_limit, 12)
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        await self.drain("StopTransaction")
        self.assertEqual(self.mqtt.values["maximum_current"], 6)

    async def test_buffered_meter_does_not_undo_later_suspension_status(self):
        await self.active()
        await self.status("SuspendedEVSE", timestamp="2026-10-08T20:00:00Z")
        await self.bridge.handle_ocpp_call(
            "MeterValues", fixtures.meter(123, "8", "5520", "2026-10-08T19:59:59Z")
        )
        self.assertEqual(self.mqtt.values["power"], 0)
        self.assertEqual(self.mqtt.values["current"], 0)

    async def test_older_status_does_not_close_newer_session(self):
        await self.active()
        await self.status("Charging", timestamp="2026-10-08T20:00:00Z")
        await self.status("Available", timestamp="2026-10-08T19:59:59Z")
        self.assertEqual(self.bridge.state.transaction_id, 123)

    async def test_invalid_persisted_target_blocks_restore_and_start_not_live_facts(self):
        self.store.valid = False
        await self.active()
        await self.drain()
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertFalse(any(a == "SetChargingProfile" for a, _ in self.connection.calls))
        with self.assertRaises(MqttCommandRejected):
            await self.command("charge_control", "ON")
        await self.command("maximum_current", 8)
        self.assertTrue(self.store.valid)

    async def test_persistence_error_keeps_live_ack_visible(self):
        await self.active()
        self.store.save = Mock(side_effect=OSError("disk full"))
        with self.assertLogs("app.bridge", "ERROR"):
            await self.command("maximum_current", 6)
        self.assertEqual(self.mqtt.values["maximum_current"], 6)

    async def test_allocator_does_not_acknowledge_disk_failure(self):
        await self.bridge.attach(self.connection)
        self.store.save = Mock(side_effect=OSError("disk full"))
        with self.assertRaises(OSError):
            await self.bridge.handle_ocpp_call("StartTransaction", {"connectorId": 1})
        self.assertIsNone(self.bridge.state.transaction_id)


class PersistenceTests(unittest.TestCase):
    def test_invalid_saved_current_is_rejected(self):
        for current in (float("nan"), float("inf"), -1, 33, True):
            store = StateStore(Path("/unused"), 16)
            with patch.object(
                Path, "read_text", return_value=json.dumps({"maximum_current": current})
            ):
                with self.assertLogs("app.state", level="ERROR"):
                    store.load()
            self.assertEqual(store.state.maximum_current, 16)
            self.assertFalse(store.valid)

    def test_invalid_saved_transaction_ids_are_rejected(self):
        for transaction_id in (True, 1.5, float("inf"), "123"):
            store = StateStore(Path("/unused"), 16)
            with patch.object(
                Path,
                "read_text",
                return_value=json.dumps(
                    {"maximum_current": 8, "last_transaction_id": transaction_id}
                ),
            ):
                with self.assertLogs("app.state", level="ERROR"):
                    store.load()
            self.assertFalse(store.valid)

    def test_atomic_round_trip_includes_closed_transaction_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory), 16)
            store.state.maximum_current = 5
            store.state.last_transaction_id = 123
            store.state.last_closed_transaction_id = 122
            store.save()
            restored = StateStore(Path(directory), 16)
            self.assertEqual(restored.load(), store.state)


class MqttRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_republishes_latest_snapshot_and_availability_last(self):
        bridge = MqttBridge.__new__(MqttBridge)
        bridge._config = SimpleNamespace(mqtt_topic_prefix="evbox_elvi")
        sent = []
        bridge._client = SimpleNamespace(
            publish=lambda topic, payload, **kwargs: (
                sent.append((topic, payload)) or SimpleNamespace(rc=0)
            ),
            subscribe=lambda *args: None,
        )
        bridge._mqtt_api = SimpleNamespace(MQTT_ERR_SUCCESS=0)
        bridge._retained_payloads = {}
        bridge._loop = asyncio.get_running_loop()
        bridge._connected = asyncio.Event()
        bridge.publish_discovery = Mock()
        bridge.publish_maximum_current(8)
        bridge.publish_charge_control(True)
        bridge.publish_charger_online(True)
        sent.clear()
        bridge._on_connect(bridge._client, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        self.assertEqual(
            sent,
            [
                ("evbox_elvi/maximum_current/state", "8"),
                ("evbox_elvi/charge_control/state", "ON"),
                ("evbox_elvi/availability", "online"),
            ],
        )
        self.assertTrue(bridge._connected.is_set())

    async def test_retained_control_command_is_not_executed(self):
        bridge = MqttBridge.__new__(MqttBridge)
        bridge._command_handler = Mock()
        bridge._loop = asyncio.get_running_loop()
        bridge._on_message(
            None,
            None,
            SimpleNamespace(topic="evbox_elvi/charge_control/set", payload=b"ON", retain=True),
        )
        bridge._command_handler.assert_not_called()
