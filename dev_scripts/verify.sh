#!/usr/bin/env bash
# shellcheck shell=bash
set -Eeuo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_directory="$(cd -- "${script_directory}/.." && pwd)"
cd "${repository_directory}"

case "$(uname -m)" in
    arm64)
        build_arch="aarch64"
        build_from="ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21-2025.11.1"
        ;;
    x86_64)
        build_arch="amd64"
        build_from="ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21-2025.11.1"
        ;;
    *)
        echo "Unsupported development architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

image_name="evbox-elvi-ocpp:test"
run_identifier="$$"
network_name="ha-ocpp-verify-${run_identifier}"
mqtt_name="ha-ocpp-verify-mqtt-${run_identifier}"
bridge_name="ha-ocpp-verify-bridge-${run_identifier}"

cleanup() {
    docker rm --force "${bridge_name}" "${mqtt_name}" >/dev/null 2>&1 || true
    docker network rm "${network_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ruff check .
shellcheck evbox_elvi_ocpp/run.sh dev_scripts/verify.sh

docker build \
    --pull \
    --build-arg "BUILD_FROM=${build_from}" \
    --build-arg "BUILD_ARCH=${build_arch}" \
    --build-arg "BUILD_VERSION=1.2.1" \
    --tag "${image_name}" \
    evbox_elvi_ocpp

docker run --rm \
    --entrypoint python3 \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --volume "${repository_directory}/tests:/tests:ro" \
    "${image_name}" \
    -m unittest discover -s /tests -v

docker network create "${network_name}" >/dev/null
docker run --detach \
    --name "${mqtt_name}" \
    --network "${network_name}" \
    --volume "${script_directory}/mosquitto-test.conf:/mosquitto/config/mosquitto.conf:ro" \
    eclipse-mosquitto:2 >/dev/null

for _ in {1..30}; do
    if docker exec "${mqtt_name}" mosquitto_pub -h localhost -t verify -m ready; then
        break
    fi
    sleep 1
done

docker run --detach \
    --name "${bridge_name}" \
    --network "${network_name}" \
    --env MQTT_HOST="${mqtt_name}" \
    --env MQTT_PORT=1883 \
    --env MQTT_USERNAME= \
    --env MQTT_PASSWORD= \
    --env EXPECTED_CHARGE_POINT_ID=EVB-P123 \
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
    "${image_name}" \
    -m app.main >/dev/null

bridge_ready=false
for _ in {1..30}; do
    if docker logs "${bridge_name}" 2>&1 | grep -q "OCPP 1.6J server listening"; then
        bridge_ready=true
        break
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' "${bridge_name}")" != "true" ]]; then
        break
    fi
    sleep 1
done

if [[ "${bridge_ready}" != "true" ]]; then
    docker logs "${bridge_name}" >&2
    echo "OCPP bridge did not become ready" >&2
    exit 1
fi

docker run --rm \
    --network "${network_name}" \
    --entrypoint python3 \
    --volume "${script_directory}/smoke_client.py:/smoke_client.py:ro" \
    "${image_name}" \
    /smoke_client.py "${bridge_name}" "${mqtt_name}"

echo "All container checks passed"
