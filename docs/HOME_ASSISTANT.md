# Home Assistant installation and migration

This is the canonical procedure for replacing the custom OCPP integration with
the EVBox Elvi OCPP bridge add-on.

The final data path is:

```text
EVBox Elvi <-- OCPP 1.6J --> add-on <-- MQTT --> Home Assistant
```

The add-on contains no solar, scheduling, charging-policy, or load-balancing
logic. Home Assistant automations continue to make every charging decision.

## Prerequisites

- Home Assistant OS or another installation that supports add-ons;
- an MQTT broker configured through the Supervisor;
- the MQTT integration active in Home Assistant with Discovery enabled;
- a recent full backup;
- access to the Elvi OCPP configuration;
- no vehicle charging during the cutover.

Two OCPP servers cannot listen on TCP port 9000 at the same time.

## Entities created

MQTT Discovery requests these exact entity IDs:

| Entity ID | Purpose |
| --- | --- |
| `switch.charger_charge_control` | Remotely start and stop a charging session |
| `switch.charger_availability` | Free/occupied state and operative availability |
| `number.charger_maximum_current` | Current limit accepted by the Elvi |
| `sensor.charger_power_active_import` | Instantaneous charging power in kW |

The `lovelace/vehicles_card.yaml` dashboard, `Safely apply current on charger`,
and the `Adapt charging power` logic use these names.

The number holds the commanded limit, not measured instantaneous current. The
power sensor uses `Power.Active.Import`, or falls back to current, voltage, and
the configured number of phases.

## Add the repository

In the Home Assistant add-on section:

1. open the store;
2. open the repositories menu;
3. add:

```text
https://github.com/davehen/ha-ocpp-server-app
```

4. refresh the store;
5. install `EVBox Elvi OCPP bridge`;
6. do not start it yet.

Leave automatic updates disabled. The project's purpose is to keep a stable
OCPP 1.6J server once it has been validated.

## Initial configuration

The default options are:

```yaml
expected_charge_point_id: ""
id_tag: HomeAssistant
heartbeat_interval: 300
meter_value_interval: 60
configure_meter_values: true
maximum_current: 16
number_of_phases: 3
command_timeout: 20
log_level: INFO
```

Keep TCP port `9000` mapped to `9000`.

- Leave `expected_charge_point_id` empty for the first start. Copy the detected
  ID from the log and save it in this option afterwards.
- `id_tag` must contain between 1 and 20 characters and is used for remote
  starts.
- `configure_meter_values` reads the Elvi configuration first and changes only
  values that differ and are writable.
- `maximum_current` is the ceiling for MQTT commands and the slider; it doesn't
  replace the electrical installation limit.
- `number_of_phases` is used only by the power fallback calculation.

The charge point ID filters the client URL, but it is not authentication. Port
9000 must remain on the trusted LAN and must never be forwarded from the
Internet.

## Device-automation limitation

Home Assistant stores internal entity-registry IDs in device-based automation
blocks, not only the visible `entity_id`. The new MQTT entities can reuse the
four names above, but they cannot inherit the internal IDs belonging to the
removed OCPP integration.

Static inspection of `davehomeassistant` found four blocks that must be
replaced:

- the `Adapt charging power` trigger;
- two `Auto-start charging` conditions;
- the `Auto-start charging` turn-on action.

`Safely apply current on charger` and `lovelace/vehicles_card.yaml` already use
the visible entity IDs and don't require changes.

### Adapt charging power trigger

Replace the device-based trigger with:

```yaml
- alias: When charging starts
  trigger: state
  entity_id: switch.charger_charge_control
  from: "off"
  to: "on"
```

### Auto-start charging conditions

Replace the two device-based conditions with:

```yaml
- alias: Car is plugged in
  condition: state
  entity_id: switch.charger_availability
  state: "off"
- alias: Car is not charging
  condition: state
  entity_id: switch.charger_charge_control
  state: "off"
```

### Auto-start charging action

Replace the device-based action with:

```yaml
- action: switch.turn_on
  target:
    entity_id: switch.charger_charge_control
```

Do not apply these changes until the new MQTT entities exist with the exact
required IDs.

## UI-managed resources to inspect

The entity-registry export also contains:

- `script.charger_charge_control_guarded`;
- `binary_sensor.car_is_charging`.

Their definitions are not present in the repository. Before the cutover, open
them in the UI and confirm that they use visible entity IDs. Replace any
device-based actions with entity actions.

## Safe cutover procedure

1. Make sure no vehicle is charging.
2. Create a full Home Assistant backup.
3. Record the version and configuration of the current OCPP integration.
4. Temporarily disable `Safely apply current on charger`, `Adapt charging
   power`, and `Auto-start charging`.
5. Install and configure the add-on without starting it.
6. Stop and remove the old OCPP integration config entry so the four entity IDs
   become free.
7. Confirm that no other process is using TCP port 9000.
8. Start the add-on.
9. Keep this URL on the Elvi:

   ```text
   ws://<HOME-ASSISTANT-IP>:9000/<CHARGE-POINT-ID>
   ```

10. Check the log for the connection, `BootNotification`, and an `Accepted`
    response.
11. Copy the ID from the log into `expected_charge_point_id`, save, and restart
    the add-on.
12. Confirm that the four entities were created without numeric suffixes.

A name such as `sensor.charger_power_active_import_2` means an old entity still
owns the required ID. Do not continue until that conflict has been resolved.

## Functional validation

Run this test under direct supervision.

1. Without a vehicle, verify availability `on`, charge control `off`, and power
   `0 kW`.
2. Connect the vehicle and verify availability `off` and charge control `off`.
3. Manually turn on `switch.charger_charge_control`.
4. Check the log for `RemoteStartTransaction`, `StartTransaction`, and a
   charging `StatusNotification`.
5. Set 6 A, 8 A, and 12 A. For each value, confirm:
   - an accepted `SetChargingProfile`;
   - an updated `number.charger_maximum_current`;
   - consistent physical current;
   - an updated `sensor.charger_power_active_import`.
6. Set 5 A and confirm the suspension behavior used by the solar automation and
   power falling to zero.
7. Restore 12 A.
8. Stop the session and verify `RemoteStopTransaction`, charge control `off`,
   and power at zero.

Apply the replacement YAML blocks only after this test passes.

## Re-enable automations

Enable them in this order:

1. `Safely apply current on charger`;
2. `Adapt charging power`;
3. `Auto-start charging`.

Supervise at least one complete solar-controlled session. Compare every
setpoint with the add-on log, the number state, and the Elvi's physical current.

## Failure behavior

- When the wallbox disconnects, MQTT entities become unavailable.
- A new current limit is published only after the Elvi accepts
  `SetChargingProfile`.
- A rejection or timeout preserves the previous value, allowing the automation
  watchdog to retry.
- A stop is rejected when no transaction ID is known.
- An unsupported OCPP action receives a `CALLERROR`.
- Malformed JSON is ignored without stopping the server.
- The last accepted current and transaction counter are saved under `/data` and
  included in add-on backups.

## Rollback

1. Disable the three charging automations.
2. Stop the add-on.
3. Restore the old integration and its configuration, or restore the complete
   backup.
4. Confirm that the four original OCPP entities return.
5. Restore the original device-based blocks if necessary.
6. Manually test start, current adjustment, and stop.
7. Re-enable automations only after the manual test succeeds.

## Related guides

- [Standalone server and manual testing](STANDALONE.md)
- [Automated build, mock, and tests](VERIFY.md)
