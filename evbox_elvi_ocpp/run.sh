#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

export MQTT_HOST
export MQTT_PORT
export MQTT_USERNAME
export MQTT_PASSWORD
export EXPECTED_CHARGE_POINT_ID
export OCPP_ID_TAG
export HEARTBEAT_INTERVAL
export METER_VALUE_INTERVAL
export CONFIGURE_METER_VALUES
export MAXIMUM_CURRENT
export NUMBER_OF_PHASES
export COMMAND_TIMEOUT
export LOG_LEVEL
export DATA_DIRECTORY=/data

MQTT_HOST="$(bashio::services mqtt host)"
MQTT_PORT="$(bashio::services mqtt port)"
MQTT_USERNAME="$(bashio::services mqtt username)"
MQTT_PASSWORD="$(bashio::services mqtt password)"
EXPECTED_CHARGE_POINT_ID="$(bashio::config expected_charge_point_id)"
OCPP_ID_TAG="$(bashio::config id_tag)"
HEARTBEAT_INTERVAL="$(bashio::config heartbeat_interval)"
METER_VALUE_INTERVAL="$(bashio::config meter_value_interval)"
CONFIGURE_METER_VALUES="$(bashio::config configure_meter_values)"
MAXIMUM_CURRENT="$(bashio::config maximum_current)"
NUMBER_OF_PHASES="$(bashio::config number_of_phases)"
COMMAND_TIMEOUT="$(bashio::config command_timeout)"
LOG_LEVEL="$(bashio::config log_level)"

exec python3 -m app.main
