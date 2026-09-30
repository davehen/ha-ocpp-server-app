# Automated build, mock, and tests

`dev_scripts/verify.sh` runs the complete verification suite without Home
Assistant or a physical wallbox.

## Prerequisites

On macOS, use the Docker CLI with Colima. Docker Desktop is not required:

```shell
brew install colima docker docker-buildx ruff shellcheck
colima start --cpu 4 --memory 6 --disk 30
```

Check the environment:

```shell
colima status
docker version
docker buildx version
ruff --version
shellcheck --version
```

The script supports `arm64` and `x86_64` hosts and automatically selects the
matching Home Assistant `aarch64` or `amd64` image.

## Run the complete verification

From the repository root:

```shell
cd /Users/davide.gallina/Development/personal/ha-ocpp-server-app
./dev_scripts/verify.sh
```

Verification succeeds only after printing:

```text
Container smoke test passed
All container checks passed
```

## What it verifies

The script performs these steps in order:

1. runs `ruff check .` on the application, tests, and mock;
2. runs ShellCheck on `run.sh` and the verification script;
3. builds the real image with Python 3.13 and the pinned Home Assistant base;
4. runs every `unittest` test inside the newly built image;
5. creates a temporary Docker network;
6. starts an anonymous Mosquitto broker confined to that network;
7. starts the bridge with `CONFIGURE_METER_VALUES=false`;
8. runs a simulated OCPP 1.6J charge point;
9. removes the temporary containers and network, even after a failure.

The build uses `--pull`, so it also verifies that the pinned base is still
available from the registry.

## What the mock does

`dev_scripts/smoke_client.py` simulates a wallbox with ID `EVB-P123` and tests
the following end-to-end behavior:

- WebSocket handshake with the `ocpp1.6` subprotocol;
- `BootNotification` and an `Accepted` response;
- the optional initial `TriggerMessage`;
- publication of a 5 A limit over MQTT;
- receipt of `SetChargingProfile` over OCPP;
- use of `TxDefaultProfile` with a `5.0` limit;
- publication of the accepted limit to
  `evbox_elvi/maximum_current/state`;
- submission of `MeterValues` with `Power.Active.Import = 2300 W`;
- publication of `2.300` to
  `evbox_elvi/power_active_import/state`.

The unit tests additionally cover remote start and stop, transaction IDs,
connector state, suspension, rejected profiles, the current-and-voltage power
fallback, OCPP response correlation, MeterValues configuration, and the exact
required entity IDs.

The mock doesn't communicate with the real Elvi, modify Home Assistant, or
expose ports to the LAN.

## Resources and cleanup

Each run uses names containing the verification process ID:

```text
ha-ocpp-verify-<pid>
ha-ocpp-verify-mqtt-<pid>
ha-ocpp-verify-bridge-<pid>
```

The cleanup function removes only those two containers and that network. It
keeps the `evbox-elvi-ocpp:test` image for standalone testing. The mock doesn't
create persistent volumes.

## Run individual checks

Unit tests without containers:

```shell
PYTHONPATH=evbox_elvi_ocpp python3 -m unittest discover -s tests -v
```

Python lint:

```shell
ruff check .
```

Shell lint:

```shell
shellcheck evbox_elvi_ocpp/run.sh dev_scripts/verify.sh
```

Shell syntax validation:

```shell
sh -n evbox_elvi_ocpp/run.sh
bash -n dev_scripts/verify.sh
```

## Troubleshooting

### Colima isn't running

```shell
colima start --cpu 4 --memory 6 --disk 30
colima status
```

### The Docker daemon doesn't respond

```shell
docker context ls
docker version
```

The active context must reach the socket reported by `colima status`.

### The base image can't be pulled

Check the corporate proxy or VPN, then try:

```shell
docker pull ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21-2025.11.1
```

On an Intel host, use the equivalent `amd64-base-python` image.

### The mock fails

The script prints bridge logs when the server doesn't become ready. To inspect
resources that might remain after a forced interruption:

```shell
docker ps --all --filter name=ha-ocpp-verify
docker network ls --filter name=ha-ocpp-verify
```

Do not remove resources with different names; they do not belong to this
verification run.
