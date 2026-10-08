"""Public bridge contract, including regressions from real Elvi sessions."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.bridge import BridgeController, extract_current_a, extract_power_kw
from app.config import Config
from app.mqtt import InvalidMqttCommand, MqttCommandRejected
from app.ocpp import OcppCallError
from app.state import PersistentState

NOW = "2026-10-08T19:45:01Z"


def config():
    return Config(
        "mqtt",
        1883,
        "",
        "",
        "",
        "HomeAssistant",
        300,
        60,
        False,
        16,
        3,
        20,
        "INFO",
        Path("/unused"),
    )


def meter(transaction_id=None, current="8", power="5520", timestamp=NOW):
    result = {
        "connectorId": 1,
        "meterValue": [
            {
                "timestamp": timestamp,
                "sampledValue": [
                    {"measurand": "Current.Import", "unit": "A", "value": current},
                    {"measurand": "Power.Active.Import", "unit": "W", "value": power},
                ],
            }
        ],
    }
    if transaction_id is not None:
        result["transactionId"] = transaction_id
    return result


class FakeMqtt:
    def __init__(self):
        self.values: dict[str, Any] = {}
        self.device_information = {}
        self.history = []

    def topic(self, suffix):
        return f"evbox_elvi/{suffix}"

    def set_command_handler(self, handler):
        self.command_handler = handler

    def _publish(self, name, value):
        self.values[name] = value
        self.history.append((name, value))

    def publish_charger_online(self, value):
        self._publish("online", value)

    def publish_charge_control(self, value):
        self._publish("charge_control", value)

    def publish_charger_availability(self, value):
        self._publish("availability", value)

    def publish_maximum_current(self, value):
        self._publish("maximum_current", value)

    def publish_power(self, value):
        self._publish("power", value)

    def publish_current(self, value):
        self._publish("current", value)

    def update_device_information(self, **values):
        self.device_information = values


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.responses = {}
        self.response_sequences = {}

    async def call(self, action, payload):
        self.calls.append((action, payload))
        sequence = self.response_sequences.get(action)
        response = (
            sequence.pop(0) if sequence else self.responses.get(action, {"status": "Accepted"})
        )
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self, **kwargs):
        self.closed = True


class FakeStateStore:
    def __init__(self, default_current):
        self.state = PersistentState(default_current)
        self.valid = True
        self.saves = 0

    def load(self):
        return self.state

    def save(self):
        self.saves += 1


class BridgeControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config, self.mqtt = config(), FakeMqtt()
        self.store = FakeStateStore(8)
        self.bridge = BridgeController(self.config, self.mqtt, self.store)
        self.connection = FakeConnection()

    async def asyncTearDown(self):
        if self.bridge._connection is not None:
            self.bridge.detach(self.bridge._connection)
        await asyncio.sleep(0)

    async def command(self, entity, value):
        await self.bridge.handle_mqtt_command(f"evbox_elvi/{entity}/set", str(value))

    async def status(self, status, **extra):
        return await self.bridge.handle_ocpp_call(
            "StatusNotification",
            {"connectorId": 1, "errorCode": "NoError", "status": status, **extra},
        )

    async def start_transaction(self):
        return await self.bridge.handle_ocpp_call(
            "StartTransaction",
            {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": NOW},
        )

    async def drain(self, action="MeterValues"):
        await self.bridge.after_ocpp_call(action)
        await self.bridge._maintenance_task

    async def active(self, transaction_id=123, current="8", power="5520"):
        await self.bridge.attach(self.connection)
        await self.bridge.handle_ocpp_call("MeterValues", meter(transaction_id, current, power))

    async def test_reconnect_invalidates_all_facts_together_but_not_target(self):
        await self.active()
        await self.command("maximum_current", 8)
        await self.bridge.attach(FakeConnection())
        for key in ("charge_control", "availability", "maximum_current", "power", "current"):
            self.assertIsNone(self.mqtt.values[key], key)
        self.assertEqual(self.store.state.maximum_current, 8)
        self.assertTrue(self.mqtt.values["online"])
        self.bridge.detach(self.connection)
        self.assertTrue(self.mqtt.values["online"])

    async def test_finishing_explicit_start_reaches_firmware(self):
        await self.bridge.attach(self.connection)
        await self.status("Finishing")
        self.assertFalse(self.mqtt.values["charge_control"])
        self.assertFalse(self.mqtt.values["availability"])
        await self.command("charge_control", "ON")
        self.assertEqual(
            [a for a, _ in self.connection.calls], ["SetChargingProfile", "RemoteStartTransaction"]
        )
        self.assertFalse(self.mqtt.values["charge_control"])

    async def test_explicit_start_has_no_local_status_whitelist(self):
        for status in (
            "Available",
            "Preparing",
            "Finishing",
            "Faulted",
            "Unavailable",
            "Reserved",
            "Charging",
        ):
            await self.bridge.attach(self.connection)
            await self.status(status)
            before = len(self.connection.calls)
            await self.command("charge_control", "ON")
            self.assertEqual(self.connection.calls[before + 1][0], "RemoteStartTransaction")

    async def test_explicit_start_with_unknown_status_uses_saved_limit(self):
        await self.bridge.attach(self.connection)
        await self.command("charge_control", "ON")
        start = self.connection.calls[-1][1]
        profile = start["chargingProfile"]
        self.assertEqual(profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"], 8)
        self.assertNotIn("transactionId", profile)
        self.assertIsNone(self.mqtt.values["charge_control"])

    async def test_new_transaction_reapplies_limit_without_waiting_for_meter(self):
        await self.bridge.attach(self.connection)
        response = await self.start_transaction()
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["maximum_current"])
        self.assertIsNone(self.mqtt.values["power"])
        await self.drain("StartTransaction")
        profile = self.connection.calls[0][1]["csChargingProfiles"]
        self.assertEqual(profile["transactionId"], response["transactionId"])
        self.assertEqual(self.mqtt.values["maximum_current"], 8)

    async def test_active_limit_then_default_use_different_scopes(self):
        await self.active()
        await self.command("maximum_current", 6)
        profiles = [
            p["csChargingProfiles"] for a, p in self.connection.calls if a == "SetChargingProfile"
        ]
        self.assertEqual(profiles[0]["transactionId"], 123)
        self.assertEqual(profiles[0]["chargingProfilePurpose"], "TxProfile")
        self.assertNotIn("transactionId", profiles[1])
        self.assertEqual(profiles[1]["chargingProfilePurpose"], "TxDefaultProfile")
        self.assertEqual(self.mqtt.values["maximum_current"], 6)
        self.assertEqual(self.mqtt.values["current"], 8)  # ACK is not a measurement.

    async def test_five_amp_pause_and_resume_keep_same_session(self):
        await self.active()
        for limit, status, measured in ((5, "SuspendedEVSE", "0"), (8, "Charging", "7.733")):
            await self.command("maximum_current", limit)
            await self.status(status)
            await self.bridge.handle_ocpp_call("MeterValues", meter(123, measured))
            self.assertEqual(self.bridge.state.transaction_id, 123)
            self.assertTrue(self.mqtt.values["charge_control"])
        self.assertFalse(
            any(
                a in {"RemoteStartTransaction", "RemoteStopTransaction", "Reset"}
                for a, _ in self.connection.calls
            )
        )

    async def test_repeated_explicit_value_is_forwarded_not_deduplicated(self):
        await self.active()
        for _ in range(3):
            await self.command("maximum_current", 8)
        self.assertEqual(len(self.connection.calls), 6)

    async def test_charging_status_is_fact_even_without_transaction_or_limit(self):
        await self.bridge.attach(self.connection)
        await self.status("Charging")
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertFalse(self.mqtt.values["availability"])
        with self.assertRaises(MqttCommandRejected):
            await self.command("maximum_current", 6)
        with self.assertRaises(MqttCommandRejected):
            await self.command("charge_control", "OFF")
        self.assertEqual(self.connection.calls, [])

    async def test_acceptance_does_not_complete_start_or_stop(self):
        await self.bridge.attach(self.connection)
        await self.status("Preparing")
        await self.command("charge_control", "ON")
        self.assertFalse(self.mqtt.values["charge_control"])
        response = await self.start_transaction()
        await self.command("charge_control", "OFF")
        self.assertTrue(self.mqtt.values["charge_control"])
        await self.bridge.handle_ocpp_call(
            "StopTransaction", {"transactionId": response["transactionId"]}
        )
        self.assertFalse(self.mqtt.values["charge_control"])
        self.assertIsNone(self.mqtt.values["availability"])

    async def test_change_availability_waits_for_observed_status(self):
        await self.bridge.attach(self.connection)
        await self.status("Available")
        self.connection.responses["ChangeAvailability"] = {"status": "Scheduled"}
        await self.command("charger_availability", "OFF")
        self.assertTrue(self.mqtt.values["availability"])
        await self.status("Unavailable")
        self.assertIsNone(self.mqtt.values["availability"])

    async def test_fault_is_not_plugged_idle_or_closed_transaction(self):
        await self.active()
        await self.status("Faulted")
        self.assertIsNone(self.mqtt.values["availability"])
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.bridge.state.transaction_id, 123)

    async def test_rejected_current_preserves_previous_confirmed_limit_and_target(self):
        await self.active()
        await self.command("maximum_current", 8)
        self.connection.responses["SetChargingProfile"] = {"status": "Rejected"}
        with self.assertRaises(MqttCommandRejected):
            await self.command("maximum_current", 6)
        self.assertEqual(self.mqtt.values["maximum_current"], 8)
        self.assertEqual(self.store.state.maximum_current, 8)

    async def test_timeout_invalidates_limit_without_changing_session_or_target(self):
        await self.active()
        await self.command("maximum_current", 8)
        self.connection.responses["SetChargingProfile"] = TimeoutError("no response")
        with self.assertRaises(TimeoutError):
            await self.command("maximum_current", 6)
        self.assertIsNone(self.mqtt.values["maximum_current"])
        self.assertTrue(self.mqtt.values["charge_control"])
        self.assertEqual(self.store.state.maximum_current, 8)

    async def test_failed_default_followup_does_not_undo_active_ack(self):
        await self.active()
        self.connection.response_sequences["SetChargingProfile"] = [
            {"status": "Accepted"},
            TimeoutError("default"),
        ]
        await self.command("maximum_current", 6)
        self.assertEqual(self.mqtt.values["maximum_current"], 6)
        self.assertIsNone(self.bridge.state.default_limit)

    async def test_invalid_mqtt_values_do_not_send_frames(self):
        await self.bridge.attach(self.connection)
        for value in ("nan", "inf", "-1", "17", "ONbh", ""):
            with self.assertRaises(InvalidMqttCommand):
                await self.command("maximum_current", value)
        for entity in ("charge_control", "charger_availability"):
            with self.assertRaises(InvalidMqttCommand):
                await self.command(entity, "ONbh")
        self.assertEqual(self.connection.calls, [])

    async def test_rejected_start_never_falls_back_to_unprofiled_start(self):
        await self.bridge.attach(self.connection)
        await self.status("Finishing")
        self.connection.responses["RemoteStartTransaction"] = {"status": "Rejected"}
        with self.assertRaises(MqttCommandRejected):
            await self.command("charge_control", "ON")
        self.assertEqual([a for a, _ in self.connection.calls].count("RemoteStartTransaction"), 1)
        self.assertFalse(self.mqtt.values["charge_control"])

    async def test_unsupported_get_configuration_does_not_gate_commands(self):
        self.bridge._config = replace(self.config, configure_meter_values=True)
        await self.bridge.attach(self.connection)
        await self.status("Finishing")
        self.connection.responses["GetConfiguration"] = OcppCallError(
            "NotSupported", "unsupported", {}
        )
        with self.assertLogs("app.bridge", "WARNING") as logs:
            await self.drain()
        self.assertTrue(all(r.exc_info is None for r in logs.records))
        await self.command("charge_control", "ON")
        self.assertEqual(self.connection.calls[-1][0], "RemoteStartTransaction")

    async def test_boot_resets_every_live_fact_not_saved_target(self):
        await self.active()
        await self.command("maximum_current", 6)
        await self.bridge.handle_ocpp_call(
            "BootNotification", {"chargePointVendor": "EV-BOX", "chargePointModel": "Elvi"}
        )
        for key in ("charge_control", "availability", "maximum_current", "power", "current"):
            self.assertIsNone(self.mqtt.values[key])
        self.assertEqual(self.store.state.maximum_current, 6)

    async def test_status_request_does_not_become_stale_when_its_status_arrives_first(self):
        await self.bridge.attach(self.connection)
        original = self.connection.call

        async def status_first(action, payload):
            if action == "TriggerMessage":
                await self.status("Finishing")
            return await original(action, payload)

        self.connection.call = status_first
        response = await self.bridge._optional_call(
            "TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 1}
        )
        self.assertEqual(response, {"status": "Accepted"})

    async def test_configuration_changes_only_different_writable_values(self):
        await self.bridge.attach(self.connection)
        self.connection.responses["GetConfiguration"] = {
            "configurationKey": [
                {
                    "key": "MeterValuesSampledData",
                    "readonly": False,
                    "value": "Energy.Active.Import.Register",
                },
                {"key": "MeterValueSampleInterval", "readonly": True, "value": "30"},
            ]
        }
        await self.bridge._ensure_configuration()
        changes = [p for a, p in self.connection.calls if a == "ChangeConfiguration"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["key"], "MeterValuesSampledData")

    async def test_central_and_invalid_status_cannot_override_connector(self):
        await self.active()
        await self.bridge.handle_ocpp_call(
            "StatusNotification", {"connectorId": 0, "status": "Available"}
        )
        await self.status("INVALID")
        self.assertTrue(self.mqtt.values["charge_control"])


class MeasurementTests(unittest.TestCase):
    def test_power_total_precedes_phases(self):
        samples = [{"measurand": "Power.Active.Import", "value": "6000"}]
        samples += [
            {"measurand": "Power.Active.Import", "value": "2000", "phase": p}
            for p in ("L1", "L2", "L3")
        ]
        self.assertEqual(extract_power_kw({"meterValue": [{"sampledValue": samples}]}, 3), 6)

    def test_invalid_samples_are_unknown_not_zero(self):
        for value in ("nan", "inf", "-1", "invalid"):
            self.assertIsNone(extract_current_a(meter(current=value)))
            self.assertIsNone(extract_power_kw(meter(current=value, power=value), 3))

    def test_power_watts_kilowatts_and_phase_sum(self):
        for unit, value in (("W", "1250"), ("kW", "1.25")):
            payload = {
                "meterValue": [
                    {
                        "sampledValue": [
                            {"measurand": "Power.Active.Import", "unit": unit, "value": value}
                        ]
                    }
                ]
            }
            self.assertEqual(extract_power_kw(payload, 3), 1.25)
        payload["meterValue"][0]["sampledValue"] = [
            {"measurand": "Power.Active.Import", "phase": p, "value": "2000"}
            for p in ("L1", "L2", "L3")
        ]
        self.assertEqual(extract_power_kw(payload, 3), 6)

    def test_current_import_is_not_offered_current(self):
        payload = {
            "meterValue": [{"sampledValue": [{"measurand": "Current.Offered", "value": "8"}]}]
        }
        self.assertIsNone(extract_current_a(payload))

    def test_current_active_phase_average_and_zero(self):
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"measurand": "Current.Import", "phase": p, "value": str(v)}
                        for p, v in (("L1", 9), ("L2", 11), ("L3", 0))
                    ]
                }
            ]
        }
        self.assertEqual(extract_current_a(payload), 10)
        for sample in payload["meterValue"][0]["sampledValue"]:
            sample["value"] = "0"
        self.assertEqual(extract_current_a(payload), 0)

    def test_unphased_current_is_authoritative(self):
        payload = meter(current="7.733")
        payload["meterValue"][0]["sampledValue"].append(
            {"measurand": "Current.Import", "phase": "L1", "value": "12"}
        )
        self.assertEqual(extract_current_a(payload), 7.733)

    def test_latest_meter_only_and_ac_fallback(self):
        payload = meter(power="1000")
        payload["meterValue"] += meter(power="2000")["meterValue"]
        self.assertEqual(extract_power_kw(payload, 3), 2)
        payload = {
            "meterValue": [
                {
                    "sampledValue": [
                        {"measurand": "Current.Import", "value": "10"},
                        {"measurand": "Voltage", "value": "230"},
                    ]
                }
            ]
        }
        self.assertEqual(extract_power_kw(payload, 3), 6.9)
        self.assertEqual(extract_power_kw(payload, 1), 2.3)

    def test_unknown_units_not_treated_as_watts_or_amperes(self):
        payload = meter(current="8", power="5520")
        for sample in payload["meterValue"][0]["sampledValue"]:
            sample["unit"] = "unknown"
        self.assertIsNone(extract_current_a(payload))
        self.assertIsNone(extract_power_kw(payload, 3))
