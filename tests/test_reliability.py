"""Regression coverage for reconnects, pending operations and persisted state."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_bridge as fixtures
from app.bridge import extract_power_kw
from app.mqtt import MqttBridge, MqttCommandRejected
from app.state import StateStore


class PendingConnection(fixtures.FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, action, payload):
        self.calls.append((action, payload))
        self.sent.set()
        await self.release.wait()
        return {"status": "Accepted"}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BridgeControllerTests.setUp
    prepare_idle = fixtures.BridgeControllerTests.prepare_idle

    async def drain_sync(self, action="MeterValues") -> None:
        await self.bridge.after_ocpp_call(action)
        if self.bridge._sync_task is not None:
            await self.bridge._sync_task
        if self.bridge._initialization_task is not None:
            await self.bridge._initialization_task

    async def recover(self, transaction_id=123, power="5400") -> None:
        await self.bridge.handle_ocpp_call(
            "MeterValues",
            {
                "connectorId": 1,
                "transactionId": transaction_id,
                "meterValue": [
                    {
                        "sampledValue": [
                            {"measurand": "Power.Active.Import", "value": power, "unit": "W"},
                            {"measurand": "Current.Import", "value": "8", "unit": "A"},
                        ]
                    }
                ],
            },
        )

    async def test_active_status_without_id_cannot_acknowledge_default_limit(self) -> None:
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Charging"}
        )
        with self.assertRaises(MqttCommandRejected):
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "6")
        self.assertEqual(self.connection.calls, [])
        self.assertIsNone(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_idle_recovery_reapplies_saved_default(self) -> None:
        self.bridge._store.state.maximum_current = 8
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        self.assertIsNone(self.mqtt.values["charge_control"])
        await self.drain_sync("StatusNotification")
        self.assertEqual(self.mqtt.values["maximum_current"], 8)
        self.assertFalse(self.mqtt.values["charge_control"])
        profiles = [
            p["csChargingProfiles"] for a, p in self.connection.calls if a == "SetChargingProfile"
        ]
        self.assertEqual(profiles[0]["chargingProfilePurpose"], "TxDefaultProfile")

    async def test_active_recovery_applies_saved_limit_before_exposing_on(self) -> None:
        self.bridge._store.state.maximum_current = 8
        await self.bridge.attach(self.connection)
        seen = []
        original = self.mqtt.publish_charge_control

        def snapshot(value):
            if value:
                seen.append((self.mqtt.values["power"], self.mqtt.values["maximum_current"]))
            original(value)

        self.mqtt.publish_charge_control = snapshot
        await self.recover()
        self.assertIsNone(self.mqtt.values["charge_control"])
        await self.drain_sync()
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertTrue(all(power == 5.4 and current == 8 for power, current in seen))
        profile = self.connection.calls[0][1]["csChargingProfiles"]
        self.assertEqual(profile["transactionId"], 123)
        self.assertEqual(profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"], 8)

    async def test_suspended_recovery_keeps_session_and_five_amp_limit(self) -> None:
        self.bridge._store.state.maximum_current = 5
        await self.bridge.attach(self.connection)
        await self.recover(power="0")
        await self.drain_sync()
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["maximum_current"], 5)
        self.assertFalse(self.mqtt.values["availability"])

    async def test_rejected_recovery_remains_unknown_and_can_retry_on_next_message(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}
        await self.drain_sync()
        self.assertIsNone(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["maximum_current"])
        self.connection.responses.clear()
        await self.drain_sync("Heartbeat")
        self.assertTrue(self.mqtt.values["charge_control"])

    async def test_remote_start_acceptance_is_not_a_started_session(self) -> None:
        await self.prepare_idle(8)
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        self.assertFalse(self.mqtt.values["charge_control"])
        with self.assertRaises(MqttCommandRejected):
            await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        self.assertIsNone(self.mqtt.values["charge_control"])
        await self.bridge.handle_ocpp_call("StartTransaction", {"connectorId": 1})
        await self.drain_sync("StartTransaction")
        self.assertIsNone(self.mqtt.values["charge_control"])
        await self.recover(self.bridge._transaction_id)
        self.assertTrue(self.mqtt.values["charge_control"])

    async def test_stop_acceptance_and_final_meter_do_not_flip_session(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        await self.drain_sync()
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "OFF")
        self.assertTrue(self.mqtt.values["charge_control"])
        await self.recover()
        self.assertTrue(self.mqtt.values["charge_control"])
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        await self.drain_sync("StopTransaction")
        self.assertFalse(self.mqtt.values["charge_control"])
        await self.recover()
        self.assertFalse(self.mqtt.values["charge_control"])
        self.assertIsNone(self.bridge._transaction_id)

    async def test_closed_transaction_is_not_recovered_on_next_connection(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        await self.bridge.attach(fixtures.FakeConnection())
        await self.recover()
        self.assertIsNone(self.bridge._transaction_id)
        self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_old_stop_and_other_transaction_meter_do_not_override_active(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover(222)
        await self.drain_sync()
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 111})
        await self.recover(111, "0")
        self.assertEqual(self.bridge._transaction_id, 222)
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.mqtt.values["power"], 5.4)

    async def test_retired_connection_cannot_change_new_session(self) -> None:
        await self.bridge.attach(self.connection)
        old = self.connection
        await self.bridge.attach(fixtures.FakeConnection())
        await self.recover(222)
        with self.assertRaises(ConnectionError):
            await self.bridge.handle_ocpp_call(
                "StopTransaction", {"transactionId": 222}, connection=old
            )
        self.assertEqual(self.bridge._transaction_id, 222)

    async def test_command_queued_on_retired_connection_is_discarded(self) -> None:
        await self.prepare_idle(8)
        await self.bridge._command_lock.acquire()
        pending = asyncio.create_task(
            self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "5")
        )
        await asyncio.sleep(0)
        new = fixtures.FakeConnection()
        await self.bridge.attach(new)
        self.bridge._command_lock.release()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertEqual(new.calls, [])

    async def test_old_profile_acknowledgment_does_not_commit_to_new_connection(self) -> None:
        old = PendingConnection()
        await self.bridge.attach(old)
        await self.recover(111)
        pending = asyncio.create_task(
            self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        )
        await old.sent.wait()
        new = fixtures.FakeConnection()
        await self.bridge.attach(new)
        await self.recover(222)
        old.release.set()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertEqual(new.calls, [])
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_active_acknowledgment_is_published_before_default_timeout(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        original = self.connection.call

        async def fail_default(action, payload):
            if (
                payload.get("csChargingProfiles", {}).get("chargingProfilePurpose")
                == "TxDefaultProfile"
            ):
                self.assertEqual(self.mqtt.values["maximum_current"], 8)
                self.assertEqual(self.bridge._store.state.maximum_current, 8)
                raise TimeoutError("mock default timeout")
            return await original(action, payload)

        self.connection.call = fail_default
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertEqual(self.mqtt.values["maximum_current"], 8)

    async def test_persistence_error_does_not_hide_accepted_live_limit(self) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        self.bridge._store.save = Mock(side_effect=OSError("mock disk full"))
        with self.assertLogs("app.bridge", level="ERROR"):
            await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertEqual(self.mqtt.values["maximum_current"], 8)

    async def test_same_confirmed_current_is_a_noop(self) -> None:
        await self.prepare_idle(8)
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertEqual(self.connection.calls, [])

    async def test_idle_profile_ack_cannot_confirm_an_unrecovered_active_session(self) -> None:
        connection = PendingConnection()
        await self.bridge.attach(connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        pending = asyncio.create_task(
            self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        )
        await connection.sent.wait()
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Charging"}
        )
        connection.release.set()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_reboot_on_same_socket_invalidates_an_inflight_idle_profile(self) -> None:
        connection = PendingConnection()
        await self.bridge.attach(connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        pending = asyncio.create_task(
            self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        )
        await connection.sent.wait()
        await self.bridge.handle_ocpp_call("BootNotification", {})
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        connection.release.set()
        with self.assertRaises(MqttCommandRejected):
            await pending
        self.assertIsNone(self.mqtt.values["maximum_current"])

    async def test_invalid_persisted_state_cannot_automatically_raise_current(self) -> None:
        self.bridge._store.valid = False
        await self.bridge.attach(self.connection)
        await self.recover()
        await self.drain_sync()
        self.assertFalse(any(a == "SetChargingProfile" for a, _ in self.connection.calls))
        self.assertIsNone(self.mqtt.values["maximum_current"])
        await self.bridge.handle_mqtt_command("evbox_elvi/maximum_current/set", "8")
        self.assertTrue(self.bridge._store.valid)
        self.assertEqual(self.mqtt.values["maximum_current"], 8)

    async def test_accepted_start_can_be_retried_after_missing_start_transaction(self) -> None:
        await self.prepare_idle(8)
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Preparing"}
        )
        with patch("app.bridge.time.monotonic", return_value=self.bridge._pending_until + 1):
            await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")
        self.assertEqual([a for a, _ in self.connection.calls].count("RemoteStartTransaction"), 2)

    async def test_stop_arriving_before_remote_stop_response_does_not_leave_pending_flag(
        self,
    ) -> None:
        await self.bridge.attach(self.connection)
        await self.recover()
        original = self.connection.call

        async def finish_first(action, payload):
            if action == "RemoteStopTransaction":
                await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
            return await original(action, payload)

        self.connection.call = finish_first
        await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "OFF")
        self.assertFalse(self.bridge._stop_pending)

    async def test_stop_on_reconnect_prevents_historical_meter_recovery(self) -> None:
        self.bridge._store.state.last_transaction_id = 123
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call("StopTransaction", {"transactionId": 123})
        await self.recover()
        self.assertIsNone(self.bridge._transaction_id)
        self.assertEqual(self.bridge._store.state.last_closed_transaction_id, 123)

    async def test_fault_is_not_mistaken_for_a_plugged_idle_car(self) -> None:
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 1, "status": "Faulted"}
        )
        await self.drain_sync("StatusNotification")
        self.assertIsNone(self.mqtt.values["availability"])
        with self.assertRaises(MqttCommandRejected):
            await self.bridge.handle_mqtt_command("evbox_elvi/charge_control/set", "ON")

    async def test_older_status_is_ignored(self) -> None:
        await self.bridge.attach(self.connection)
        for status, timestamp in (
            ("Charging", "2026-10-08T16:20:00Z"),
            ("Available", "2026-10-08T16:00:00Z"),
        ):
            await self.bridge.handle_ocpp_call(
                "StatusNotification", {"connectorId": 1, "status": status, "timestamp": timestamp}
            )
        self.assertEqual(self.bridge._status, "Charging")

    async def test_older_meter_does_not_overwrite_newer_measurement(self) -> None:
        await self.bridge.attach(self.connection)
        for value, timestamp in (("5000", "2026-10-08T16:20:00Z"), ("0", "2026-10-08T16:00:00Z")):
            await self.bridge.handle_ocpp_call(
                "MeterValues",
                {
                    "connectorId": 1,
                    "meterValue": [
                        {
                            "timestamp": timestamp,
                            "sampledValue": [{"measurand": "Power.Active.Import", "value": value}],
                        }
                    ],
                },
            )
        self.assertEqual(self.mqtt.values["power"], 5)


class PersistenceTests(unittest.TestCase):
    def test_invalid_saved_current_is_rejected(self) -> None:
        for current in (float("nan"), float("inf"), -1, 33, True):
            store = StateStore(Path("/unused"), 16)
            with patch.object(
                Path, "read_text", return_value=json.dumps({"maximum_current": current})
            ):
                with self.assertLogs("app.state", level="ERROR"):
                    store.load()
            self.assertEqual(store.state.maximum_current, 16)
            self.assertFalse(store.valid)

    def test_invalid_saved_transaction_ids_are_rejected_without_crashing(self) -> None:
        for transaction_id in (True, 1.5, float("inf"), "123"):
            store = StateStore(Path("/unused"), 16)
            with patch.object(
                Path,
                "read_text",
                return_value=json.dumps(
                    {
                        "maximum_current": 8,
                        "last_transaction_id": transaction_id,
                    }
                ),
            ):
                with self.assertLogs("app.state", level="ERROR"):
                    store.load()
            self.assertFalse(store.valid)

    def test_atomic_round_trip_includes_closed_transaction_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory), 16)
            store.state.maximum_current = 5
            store.state.last_transaction_id = 123
            store.state.last_closed_transaction_id = 122
            store.save()
            restored = StateStore(Path(directory), 16)
            self.assertEqual(restored.load(), store.state)


class PowerReliabilityTests(unittest.TestCase):
    def test_total_and_phases_are_not_double_counted(self) -> None:
        samples = [{"measurand": "Power.Active.Import", "value": "6000"}]
        samples.extend(
            {"measurand": "Power.Active.Import", "value": "2000", "phase": p}
            for p in ("L1", "L2", "L3")
        )
        self.assertEqual(extract_power_kw({"meterValue": [{"sampledValue": samples}]}, 3), 6)

    def test_invalid_power_is_not_reported_as_zero(self) -> None:
        for value in ("nan", "inf", "-1"):
            self.assertIsNone(
                extract_power_kw(
                    {
                        "meterValue": [
                            {"sampledValue": [{"measurand": "Power.Active.Import", "value": value}]}
                        ]
                    },
                    3,
                )
            )


class MqttRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_republishes_latest_snapshot_and_availability_last(self) -> None:
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

    async def test_retained_control_command_is_not_executed(self) -> None:
        bridge = MqttBridge.__new__(MqttBridge)
        bridge._command_handler = Mock()
        bridge._loop = asyncio.get_running_loop()
        bridge._on_message(
            None,
            None,
            SimpleNamespace(topic="evbox_elvi/charge_control/set", payload=b"ON", retain=True),
        )
        bridge._command_handler.assert_not_called()
