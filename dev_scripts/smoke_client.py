"""Real WebSocket/MQTT regression test; never connects to a real charger."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import suppress
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from websockets.legacy.client import connect


class MockWallbox:
    """Receive responses and server calls concurrently, as a real Elvi does."""

    def __init__(self, websocket, changed, transaction_id=None):
        self.websocket = websocket
        self.changed = changed
        self.transaction_id = transaction_id
        self.current = 0
        self.status = "Finishing" if transaction_id is None else "SuspendedEVSE"
        self.commands, self.pending = [], {}
        self.sequence = 0
        self.reader = asyncio.create_task(self.read())

    async def send(self, frame):
        await self.websocket.send(json.dumps(frame))

    async def call(self, action, payload):
        self.sequence += 1
        message_id = f"mock-{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        try:
            await self.send([2, message_id, action, payload])
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(message_id, None)

    async def read(self):
        async for message in self.websocket:
            frame = json.loads(message)
            self.changed.set()
            if frame[0] in (3, 4):
                future = self.pending.get(frame[1])
                if future is not None and not future.done():
                    if frame[0] == 3:
                        if "transactionId" in frame[2]:
                            self.transaction_id = frame[2]["transactionId"]
                        future.set_result(frame[2])
                    else:
                        future.set_exception(AssertionError(f"Server rejected mock CALL: {frame}"))
            elif frame[0] == 2:
                _, message_id, action, payload = frame
                self.commands.append((action, payload))
                if action == "GetConfiguration":
                    await self.send([4, message_id, "NotSupported", "Legacy firmware", {}])
                    continue
                if action == "SetChargingProfile":
                    profile = payload["csChargingProfiles"]
                    if profile["chargingProfilePurpose"] == "TxProfile":
                        assert profile["transactionId"] == self.transaction_id
                        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]
                        self.current = 0 if limit < 6 else limit
                        self.status = "SuspendedEVSE" if self.current == 0 else "Charging"
                await self.send([3, message_id, {"status": "Accepted"}])
                if action == "TriggerMessage":
                    if payload["requestedMessage"] == "StatusNotification":
                        await self.notify_status()
                    elif payload["requestedMessage"] == "MeterValues":
                        self.sequence += 1
                        await self.send([2, f"meter-{self.sequence}", "MeterValues", self.meter()])

    async def notify_status(self):
        # Do not wait for a CALLRESULT in the sole receive task.
        self.sequence += 1
        await self.send(
            [
                2,
                f"status-{self.sequence}",
                "StatusNotification",
                {
                    "connectorId": 1,
                    "status": self.status,
                    "errorCode": "NoError",
                },
            ]
        )

    def meter(self):
        payload = {
            "connectorId": 1,
            "meterValue": [
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "sampledValue": [
                        {"value": str(self.current), "measurand": "Current.Import", "unit": "A"},
                        {
                            "value": str(self.current * 690),
                            "measurand": "Power.Active.Import",
                            "unit": "W",
                        },
                    ],
                }
            ],
        }
        if self.transaction_id is not None:
            payload["transactionId"] = self.transaction_id
        return payload

    async def close(self):
        self.reader.cancel()
        with suppress(asyncio.CancelledError):
            await self.reader


async def run(bridge_host, mqtt_host):
    states, subscribed = {}, threading.Event()
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def wait_until(predicate, description):
        try:
            async with asyncio.timeout(10):
                while True:
                    changed.clear()
                    if predicate():
                        return
                    await changed.wait()
        except TimeoutError:
            raise AssertionError(f"Timed out waiting for {description}") from None

    def on_connect(client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe("evbox_elvi/#", qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        subscribed.set()

    def on_message(client, userdata, message):
        loop.call_soon_threadsafe(
            record_state, message.topic.removeprefix("evbox_elvi/"), message.payload.decode()
        )

    def record_state(topic, payload):
        states[topic] = payload
        changed.set()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="evbox-elvi-verifier"
    )
    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    client.connect(mqtt_host, 1883, keepalive=30)
    client.loop_start()

    async def command(suffix, value):
        result = client.publish(f"evbox_elvi/{suffix}/set", str(value), qos=1, retain=False)
        await asyncio.to_thread(result.wait_for_publish, 5)

    async def state(suffix, expected):
        await wait_until(lambda: states.get(suffix) == expected, f"{suffix}={expected}")

    async def set_current(wallbox, limit):
        before = len(wallbox.commands)
        await command("maximum_current", limit)
        await state("maximum_current/state", str(limit))
        await wait_until(
            lambda: (
                len([a for a, _ in wallbox.commands[before:] if a == "SetChargingProfile"])
                >= (2 if wallbox.transaction_id is not None else 1)
            ),
            "current profiles",
        )
        profiles = [
            p["csChargingProfiles"]
            for a, p in wallbox.commands[before:]
            if a == "SetChargingProfile"
        ]
        if wallbox.transaction_id is not None:
            assert profiles[0]["chargingProfilePurpose"] == "TxProfile"
            assert profiles[0]["transactionId"] == wallbox.transaction_id
        assert profiles[-1]["chargingProfilePurpose"] == "TxDefaultProfile"

    try:
        if not await asyncio.to_thread(subscribed.wait, 5):
            raise AssertionError("MQTT subscription failed")
        uri = f"ws://{bridge_host}:9000/EVB-P123"
        async with connect(uri, subprotocols=["ocpp1.6"]) as websocket:
            wallbox = MockWallbox(websocket, changed)
            try:
                response = await wallbox.call(
                    "BootNotification",
                    {
                        "chargePointVendor": "EV-BOX",
                        "chargePointModel": "Elvi",
                        "chargePointSerialNumber": "EVB-P123",
                        "firmwareVersion": "mock",
                    },
                )
                assert response["status"] == "Accepted"
                await state("maximum_current/state", "16")
                await state("charge_control/state", "OFF")
                await state("charger_availability/state", "OFF")
                await set_current(wallbox, 8)
                await command("charge_control", "ON")
                await wait_until(
                    lambda: any(a == "RemoteStartTransaction" for a, _ in wallbox.commands),
                    "RemoteStartTransaction",
                )
                start = next(p for a, p in wallbox.commands if a == "RemoteStartTransaction")
                profile = start["chargingProfile"]
                assert profile["chargingProfilePurpose"] == "TxProfile"
                assert "transactionId" not in profile
                assert profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] == 8
                assert states["charge_control/state"] == "OFF"
                start_commands = len(wallbox.commands)
                response = await wallbox.call(
                    "StartTransaction",
                    {
                        "connectorId": 1,
                        "idTag": "HomeAssistant",
                        "meterStart": 0,
                        "timestamp": "2026-10-08T16:00:00Z",
                    },
                )
                transaction_id = wallbox.transaction_id = response["transactionId"]
                wallbox.status = "Charging"
                await wallbox.notify_status()
                await state("maximum_current/state", "8")
                await wait_until(lambda: wallbox.current == 8, "start-time active TxProfile")
                await wait_until(
                    lambda: (
                        len(
                            [
                                a
                                for a, _ in wallbox.commands[start_commands:]
                                if a == "SetChargingProfile"
                            ]
                        )
                        >= 2
                    ),
                    "start-time default follow-up",
                )
                await wallbox.call("MeterValues", wallbox.meter())
                await state("charge_control/state", "ON")
                await state("current_import/state", "8.000")
                await state("power_active_import/state", "5.520")
                for limit in (6, 5, 8):
                    await set_current(wallbox, limit)
                    await wallbox.call("MeterValues", wallbox.meter())
                    await state("current_import/state", f"{wallbox.current:.3f}")
                    assert states["charge_control/state"] == "ON"
                    assert wallbox.transaction_id == transaction_id
                await set_current(wallbox, 5)
            finally:
                await wallbox.close()
        await state("availability", "offline")
        async with connect(uri, subprotocols=["ocpp1.6"]) as websocket:
            wallbox = MockWallbox(websocket, changed, transaction_id)
            try:
                await state("maximum_current/state", "None")
                await wallbox.call("MeterValues", wallbox.meter())  # No BootNotification.
                await state("maximum_current/state", "5")
                await state("charge_control/state", "ON")
                await state("power_active_import/state", "0.000")
                await state("availability", "online")
                await set_current(wallbox, 8)
                await wallbox.call("MeterValues", wallbox.meter())
                await state("power_active_import/state", "5.520")
                await command("charge_control", "OFF")
                await wait_until(
                    lambda: any(a == "RemoteStopTransaction" for a, _ in wallbox.commands),
                    "RemoteStopTransaction",
                )
                assert states["charge_control/state"] == "ON"
                await wallbox.call(
                    "StopTransaction",
                    {
                        "transactionId": transaction_id,
                        "meterStop": 0,
                        "timestamp": "2026-10-08T16:10:00Z",
                    },
                )
                wallbox.transaction_id = None
                wallbox.current, wallbox.status = 0, "Finishing"
                await wallbox.notify_status()
                await state("charge_control/state", "OFF")
                await state("maximum_current/state", "8")
                await state("power_active_import/state", "0.000")
                historical = wallbox.meter()
                historical["transactionId"] = transaction_id
                await wallbox.call("MeterValues", historical)
                assert states["charge_control/state"] == "OFF"
                # This was the regression that blocked real HA auto-start.
                await command("charge_control", "ON")
                await wait_until(
                    lambda: any(a == "RemoteStartTransaction" for a, _ in wallbox.commands),
                    "remote start from Finishing after stop",
                )
            finally:
                await wallbox.close()
    finally:
        client.disconnect()
        client.loop_stop()
    print(
        "Container smoke passed: Finishing start, NotSupported, 8/6/5/8 A, recovery, confirmed stop"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: smoke_client.py BRIDGE_HOST MQTT_HOST")
    asyncio.run(run(sys.argv[1], sys.argv[2]))
