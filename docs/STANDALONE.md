# Standalone server and manual testing

This guide runs the bridge outside Home Assistant using the Docker CLI and
Colima. Docker Desktop is neither required nor used.

The resulting environment is:

```text
EVBox Elvi <-- OCPP 1.6J --> bridge <-- MQTT --> Mosquitto
```

The bridge contains no solar, charging-policy, scheduling, or load-balancing
logic. It only translates OCPP messages and MQTT commands.

## Prerequisites

Install the tools once:

```shell
brew install colima docker docker-buildx ruff shellcheck
```

Start Colima:

```shell
colima start --cpu 4 --memory 6 --disk 30
colima status
docker version
```

`colima status` must report `colima is running` with the `docker` runtime.

## Build the image

From the repository root on Apple Silicon:

```shell
cd /Users/davide.gallina/Development/personal/ha-ocpp-server-app

docker build \
  --pull \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21-2025.11.1 \
  --build-arg BUILD_ARCH=aarch64 \
  --build-arg BUILD_VERSION=1.2.1 \
  --tag evbox-elvi-ocpp:test \
  evbox_elvi_ocpp
```

On an Intel Mac, replace `aarch64` with `amd64` in both `BUILD_FROM` and
`BUILD_ARCH`.

Alternatively, `./dev_scripts/verify.sh` builds the same image and runs all the
checks documented in [VERIFY.md](VERIFY.md).

## Start the MQTT broker

Create a dedicated network and start Mosquitto:

```shell
docker network create evbox-elvi-test

docker run --detach \
  --name evbox-test-mqtt \
  --network evbox-elvi-test \
  --volume "$PWD/dev_scripts/mosquitto-test.conf:/mosquitto/config/mosquitto.conf:ro" \
  eclipse-mosquitto:2
```

The broker isn't exposed to the LAN. The commands in this guide use the
`mosquitto_pub` and `mosquitto_sub` utilities already included in the
container.

## Start the bridge

The add-on `/run.sh` script uses `bashio` and requires the Supervisor. In
standalone mode, start `python3 -m app.main` directly.

For the first test, leave `EXPECTED_CHARGE_POINT_ID` empty and disable automatic
MeterValues configuration:

```shell
docker volume create evbox-test-data

docker run --detach \
  --name evbox-test-bridge \
  --network evbox-elvi-test \
  --publish 9000:9000 \
  --volume evbox-test-data:/data \
  --env MQTT_HOST=evbox-test-mqtt \
  --env MQTT_PORT=1883 \
  --env MQTT_USERNAME= \
  --env MQTT_PASSWORD= \
  --env EXPECTED_CHARGE_POINT_ID= \
  --env OCPP_ID_TAG=HomeAssistant \
  --env HEARTBEAT_INTERVAL=300 \
  --env METER_VALUE_INTERVAL=60 \
  --env CONFIGURE_METER_VALUES=false \
  --env MAXIMUM_CURRENT=16 \
  --env NUMBER_OF_PHASES=3 \
  --env COMMAND_TIMEOUT=20 \
  --env LOG_LEVEL=INFO \
  --env DATA_DIRECTORY=/data \
  --entrypoint python3 \
  evbox-elvi-ocpp:test \
  -m app.main
```

Follow the logs in another terminal:

```shell
docker logs --follow evbox-test-bridge
```

## Connect the wallbox

Temporarily configure the Elvi OCPP server URL as:

```text
ws://<MAC-IP>:9000/<CHARGE-POINT-ID>
```

For example:

```text
ws://192.168.1.20:9000/EVB-P123
```

The charge point ID is the final URL segment. To find the Mac Wi-Fi address:

```shell
ipconfig getifaddr en0
```

The bridge log must show the accepted OCPP connection. A reconnecting Elvi may
resume with `Heartbeat` or `MeterValues` without sending `BootNotification`;
MQTT availability becomes `online` in either case. After confirming the ID, recreate the container with
`EXPECTED_CHARGE_POINT_ID` set to that value. This rejects clients using a
different ID, but it is not authentication: never expose port 9000 to the
Internet.

## Observe state

Subscribe to all runtime topics:

```shell
docker exec -it evbox-test-mqtt \
  mosquitto_sub -h localhost -v -t 'evbox_elvi/#'
```

The primary state topics are:

| Topic | Meaning |
| --- | --- |
| `evbox_elvi/availability` | `online` while an accepted OCPP connection is active; `offline` on disconnect |
| `evbox_elvi/charge_control/state` | Charging-session state |
| `evbox_elvi/charger_availability/state` | `ON` when the connector is free; `OFF` when occupied or unavailable |
| `evbox_elvi/maximum_current/state` | Last current limit accepted by the Elvi |
| `evbox_elvi/current_import/state` | Measured charging current in A |
| `evbox_elvi/power_active_import/state` | Measured charging power in kW |

Subscribe specifically to the accepted current limit, measured current, and
measured power:

```shell
docker exec -it evbox-test-mqtt mosquitto_sub -h localhost -v \
  -t evbox_elvi/maximum_current/state \
  -t evbox_elvi/current_import/state \
  -t evbox_elvi/power_active_import/state
```

To read only the latest retained value and exit, use these one-shot commands.
They query the standalone MQTT broker directly; Home Assistant is not involved:

```shell
# Measured charging current in A
docker exec evbox-test-mqtt \
  mosquitto_sub -h localhost -C 1 -W 2 \
  -t evbox_elvi/current_import/state

# Measured charging power in kW
docker exec evbox-test-mqtt \
  mosquitto_sub -h localhost -C 1 -W 2 \
  -t evbox_elvi/power_active_import/state

# Current limit last accepted by the Elvi in A
docker exec evbox-test-mqtt \
  mosquitto_sub -h localhost -C 1 -W 2 \
  -t evbox_elvi/maximum_current/state
```

`-C 1` exits after the first value and `-W 2` stops waiting after two seconds
if no retained value is available. The measured current and power are the
latest samples received from the Elvi, not active OCPP queries. Their age is
therefore limited by the configured `MeterValues` interval, normally 60
seconds in this standalone setup.

The maximum-current number is the commanded and accepted limit, not the
instantaneous measurement. `current_import/state` comes from the latest
`Current.Import` sample. If the Elvi reports one value per phase, the bridge
publishes the average of the active phases. Power comes from
`Power.Active.Import`, or is derived from current and voltage when that
measurand is absent.

Version 1.2.1 doesn't yet publish separate sensors for cumulative energy or
session energy. Do not treat `maximum_current/state` as an
instantaneous-current measurement.

Subscribe to MQTT Discovery messages with:

```shell
docker exec -it evbox-test-mqtt \
  mosquitto_sub -h localhost -v -t 'homeassistant/#'
```

## Available commands

These are all the public MQTT commands implemented by the bridge:

| Topic | Payload | OCPP effect |
| --- | --- | --- |
| `evbox_elvi/charge_control/set` | `ON` | `RemoteStartTransaction` |
| `evbox_elvi/charge_control/set` | `OFF` | `RemoteStopTransaction` |
| `evbox_elvi/charger_availability/set` | `ON` | `ChangeAvailability: Operative` |
| `evbox_elvi/charger_availability/set` | `OFF` | `ChangeAvailability: Inoperative` |
| `evbox_elvi/maximum_current/set` | number | Idle: `TxDefaultProfile`; active transaction: `TxProfile` plus a best-effort default update |

The current limit must be finite and between 0 and `MAXIMUM_CURRENT`. The Elvi
may reject values unsupported by its firmware. A value of 5 A is allowed to
preserve the suspension behavior used by the solar automation.

When no transaction ID is known, the command sets the connector's
`TxDefaultProfile`. During a transaction, it first sends a higher-stack
`TxProfile` bound to that transaction ID so the limit applies to the active
session. Only after that profile is accepted does it update the default for the
next session.

### Adjust the current limit

Start the state subscriber first, then test 6, 8, and 12 A in that order:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 6

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 8

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 12
```

For every command, verify:

1. an accepted transaction-bound `TxProfile` in the log while charging;
2. an updated `maximum_current/state`;
3. a corresponding change in `current_import/state` and physical current;
4. consistent measured power.

Test 5 A only after the normal values:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 5
```

The test must show the suspension behavior already used by Home Assistant and
measured current and power falling to zero.

If no power is received because the Elvi doesn't already have the required
MeterValues configured, recreate the bridge with
`CONFIGURE_METER_VALUES=true`. The bridge reads the existing configuration
first and changes only values that differ and aren't read-only.

### Start and stop charging

With the vehicle connected:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charge_control/set -m ON
```

Wait for `StartTransaction`, then stop:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charge_control/set -m OFF
```

The stop command is rejected if the bridge has not received a transaction ID
from the wallbox yet.

### Change availability

Use these commands only after validating start, stop, and current control:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charger_availability/set -m OFF

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charger_availability/set -m ON
```

`OFF` makes the connector `Inoperative`; `ON` returns it to `Operative`.

## Recommended test sequence

1. Start the broker and bridge without a vehicle connected.
2. Connect the Elvi and verify that `evbox_elvi/availability` becomes `online`.
3. Check availability `ON`, charge control `OFF`, and power `0.000`.
4. Connect the vehicle and verify availability `OFF`.
5. Start the charging session.
6. Test 6, 8, and 12 A while checking logs, MQTT state, and physical behavior.
7. Test 5 A and confirm suspension.
8. Restore 12 A, then stop the session.
9. Verify charge control `OFF` and power `0.000`.

## Stop and clean up

```shell
docker stop evbox-test-bridge evbox-test-mqtt
docker rm evbox-test-bridge evbox-test-mqtt
docker network rm evbox-elvi-test
```

The `evbox-test-data` volume is intentionally retained. To also remove the
persisted current and transaction counter:

```shell
docker volume rm evbox-test-data
```

The last command erases the lab state and isn't required between runs.
