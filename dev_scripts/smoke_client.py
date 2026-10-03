"""End-to-end OCPP/MQTT smoke client for the development container."""

from __future__ import annotations

import asyncio
import json
import sys
import threading

import paho.mqtt.client as mqtt
from websockets.legacy.client import connect


async def receive_command(websocket, expected_action: str) -> list[object]:
    """Acknowledge initialization calls until the requested command arrives."""
    while True:
        frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
        if frame[0] != 2:
            raise AssertionError(f"Expected an OCPP CALL, received {frame!r}")
        if frame[2] == expected_action:
            return frame
        if frame[2] == "TriggerMessage":
            await websocket.send(json.dumps([3, frame[1], {"status": "Accepted"}]))
            continue
        raise AssertionError(f"Unexpected OCPP action {frame[2]!r}")


async def run(bridge_host: str, mqtt_host: str) -> None:
    """Exercise one boot, current command, and meter-value round trip."""
    current_received = threading.Event()
    measured_current_received = threading.Event()
    power_received = threading.Event()
    subscribed = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            raise RuntimeError(f"MQTT connection failed: {reason_code}")
        client.subscribe(
            [
                ("evbox_elvi/maximum_current/state", 1),
                ("evbox_elvi/current_import/state", 1),
                ("evbox_elvi/power_active_import/state", 1),
            ]
        )

    def on_subscribe(client, userdata, mid, reason_codes, properties) -> None:
        subscribed.set()

    def on_message(client, userdata, message) -> None:
        payload = message.payload.decode()
        if message.topic == "evbox_elvi/maximum_current/state" and payload == "5":
            current_received.set()
        if message.topic == "evbox_elvi/current_import/state" and payload == "10.000":
            measured_current_received.set()
        if message.topic == "evbox_elvi/power_active_import/state" and payload == "2.300":
            power_received.set()

    mqtt_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="evbox-elvi-verifier",
    )
    mqtt_client.on_connect = on_connect
    mqtt_client.on_subscribe = on_subscribe
    mqtt_client.on_message = on_message
    mqtt_client.connect(mqtt_host, 1883, keepalive=30)
    mqtt_client.loop_start()

    try:
        if not await asyncio.to_thread(subscribed.wait, 5):
            raise TimeoutError("MQTT subscriptions were not acknowledged")

        uri = f"ws://{bridge_host}:9000/EVB-P123"
        async with connect(uri, subprotocols=["ocpp1.6"]) as websocket:
            await websocket.send(
                json.dumps(
                    [
                        2,
                        "boot-1",
                        "BootNotification",
                        {
                            "chargePointVendor": "EV-BOX",
                            "chargePointModel": "Elvi",
                            "chargePointSerialNumber": "EVB-P123",
                            "firmwareVersion": "smoke-test",
                        },
                    ]
                )
            )
            boot_response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            assert boot_response[0:2] == [3, "boot-1"]
            assert boot_response[2]["status"] == "Accepted"

            publish_result = mqtt_client.publish(
                "evbox_elvi/maximum_current/set",
                "5",
                qos=1,
            )
            publish_result.wait_for_publish(timeout=5)

            command = await receive_command(websocket, "SetChargingProfile")
            profile = command[3]["csChargingProfiles"]
            assert profile["chargingProfilePurpose"] == "TxDefaultProfile"
            assert profile["chargingSchedule"]["chargingSchedulePeriod"] == [
                {"startPeriod": 0, "limit": 5.0}
            ]
            await websocket.send(json.dumps([3, command[1], {"status": "Accepted"}]))
            if not await asyncio.to_thread(current_received.wait, 5):
                raise TimeoutError("Accepted 5 A state was not published through MQTT")

            await websocket.send(
                json.dumps(
                    [
                        2,
                        "meter-1",
                        "MeterValues",
                        {
                            "connectorId": 1,
                            "meterValue": [
                                {
                                    "timestamp": "2026-09-30T12:00:00Z",
                                    "sampledValue": [
                                        {
                                            "value": "2300",
                                            "measurand": "Power.Active.Import",
                                            "unit": "W",
                                        },
                                        {
                                            "value": "10",
                                            "measurand": "Current.Import",
                                            "unit": "A",
                                        },
                                    ],
                                }
                            ],
                        },
                    ]
                )
            )
            meter_response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            assert meter_response == [3, "meter-1", {}]
            if not await asyncio.to_thread(power_received.wait, 5):
                raise TimeoutError("2.300 kW meter value was not published through MQTT")
            if not await asyncio.to_thread(measured_current_received.wait, 5):
                raise TimeoutError("10.000 A meter value was not published through MQTT")
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()

    print("Container smoke test passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: smoke_client.py BRIDGE_HOST MQTT_HOST")
    asyncio.run(run(sys.argv[1], sys.argv[2]))
