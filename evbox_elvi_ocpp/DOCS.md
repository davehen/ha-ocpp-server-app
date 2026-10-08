# EVBox Elvi OCPP bridge

The canonical, step-by-step installation and migration procedure is maintained
in [the Home Assistant guide](../docs/HOME_ASSISTANT.md). The reference below
is kept with the add-on so it remains available from the app-store
Documentation tab.

## Purpose

This Home Assistant app is a deliberately frozen compatibility layer for one
EVBox Elvi that speaks OCPP 1.6J. It exposes only the entities needed by the
current Home Assistant configuration and contains no charging policy.

The data path is:

```text
EVBox Elvi <-- OCPP 1.6J --> this app <-- MQTT --> Home Assistant
```

Home Assistant automations remain responsible for deciding when charging
starts, stops, or changes current.

## Exposed entities

After the old OCPP integration has been removed and its entity IDs are free,
MQTT Discovery requests these exact IDs:

| Entity ID | OCPP mapping |
| --- | --- |
| `switch.charger_charge_control` | `RemoteStartTransaction` / `RemoteStopTransaction` |
| `switch.charger_availability` | `ChangeAvailability` and `StatusNotification` |
| `number.charger_maximum_current` | `TxProfile` while charging; `TxDefaultProfile` while idle |
| `sensor.charger_current_import` | Measured `Current.Import` in amperes |
| `sensor.charger_power_active_import` | `Power.Active.Import`, with a current/voltage fallback |

Before a transaction starts, the current limit uses a connector-level
`TxDefaultProfile`. During an active transaction, the app first sends a
higher-stack `TxProfile` containing the Elvi's current `transactionId`; this is
the profile that applies to the active transaction. After that profile is
accepted, the app also updates `TxDefaultProfile` as a best-effort follow-up so
the same limit applies to the next transaction.

## Configuration

The defaults match the current installation:

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

- Leave `expected_charge_point_id` empty for the first connection. The app logs
  the ID taken from the final segment of the wallbox URL. After validation, the
  logged ID can be copied into this option to reject every other client.
  This is a client filter, not authentication: keep TCP port `9000` on the
  trusted local network and never forward it from the Internet.
- `id_tag` must contain 1 to 20 characters. It is sent only with a remote start.
- `configure_meter_values` reads the Elvi configuration and changes only values
  that differ from the standardized set needed by the meter sensors. A
  rejection is logged but does not stop the OCPP server.
- `maximum_current` is a safety ceiling for MQTT commands and the Home Assistant
  slider; it does not alter the wallbox's electrical installation limit.
- `number_of_phases` is used only if the wallbox omits
  `Power.Active.Import` and power must be derived from current and voltage.

The maximum-current number is a commanded limit. The measured-current sensor
uses the latest `Current.Import` sample directly when it has no phase. For
per-phase samples, it reports the average of the active L1/L2/L3 values; an
all-zero sample reports `0 A`.

## Important migration limitation

Home Assistant device automations store internal entity-registry IDs, not the
visible `entity_id`. A new MQTT entity cannot inherit the internal ID of an OCPP
custom-integration entity. The visible IDs can remain identical, but the four
device-based blocks below must be recreated as state/action blocks.

The original migration affected these blocks (the current October 8 YAML
already contains their state/action replacements):

- `Adapt charging power`: its start trigger uses the old charge-control device
  entity.
- `Auto-start charging`: its plugged-in condition, not-charging condition, and
  turn-on action use the old OCPP device entity.

`Safely apply current on charger` and `lovelace/vehicles_card.yaml` already use
the visible entity IDs. Before enabling 1.4.0 automation, apply the mandatory
missing-data guards in [the canonical HA guide](../docs/HOME_ASSISTANT.md#mandatory-missing-data-guards-in-adapt-charging-power):
skip solar calculations while either power input is unknown/unavailable;
use `float(none)` for the accepted current and treat an unknown limit as needing
an explicit current retry. Keep the existing watchdog delay outside the guard.
Session ON is an observation, not a readiness signal for other entities.
The HA repository is not modified by this add-on.

The exported registry also contains `script.charger_charge_control_guarded`,
but its definition is not present in the repository. Inspect that UI-managed
script before migration and replace any device action with an entity action.
The same limitation applies to the UI-managed template
`binary_sensor.car_is_charging`: confirm in Home Assistant that its template
uses preserved entity IDs rather than an internal OCPP device reference.

### Replacement blocks

Replace the triggers in `Adapt charging power` and add the state condition below;
preserve its existing actions and `mode: single`. This also replaces a previously
migrated `switch.turned_on` trigger so recovered sessions can resume automation:

```yaml
triggers:
  - alias: When a charging session becomes ready
    trigger: state
    entity_id: switch.charger_charge_control
    to: "on"
  - alias: Resume after Home Assistant startup
    trigger: homeassistant
    event: start
conditions:
  - condition: state
    entity_id: switch.charger_charge_control
    state: "on"
```

No `from: "off"`: recovery from unknown/unavailable must also trigger it. The
bridge does not fabricate OFF/ON cycles to restart HA automations.

Replace the two charger device conditions in `Auto-start charging` with:

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

Replace its device action with:

```yaml
- action: switch.turn_on
  target:
    entity_id: switch.charger_charge_control
```

Do not apply these replacements until the MQTT entities have been created with
the expected IDs.

## Safe installation and validation

Charging control is safety-critical. Perform the cutover while no vehicle is
charging and preserve a Home Assistant backup.

1. Add this GitHub repository as a Home Assistant app repository.
2. Install the app, but do not start it. Keep TCP port `9000` mapped to `9000`.
3. Record the installed version of the old OCPP integration and make a Home
   Assistant backup. This is the rollback point.
4. Disable the three charging automations temporarily.
5. Stop the old OCPP integration and remove its configuration entry so the five
   existing entity IDs become free. Do not run both servers on port 9000.
6. Start this app. The wallbox URL remains
   `ws://<home-assistant-ip>:9000/<charge-point-id>` and therefore should not
   need to change.
7. Confirm in the app log that the wallbox connects and verify that
   `evbox_elvi/availability` becomes `online`. A reconnecting Elvi may send
   `Heartbeat` or `MeterValues` without a new `BootNotification`.
8. Confirm that all five entity IDs in the table exist without a numeric suffix.
   A suffix such as `_2` means an old entity still owns the required ID; stop
   here and resolve the registry conflict.
9. With no vehicle connected, verify that
   `switch.charger_availability` is `on`, charge control is `off`, and power is
   `0 kW`.
10. Connect a vehicle. Verify that availability changes to `off` while charge
    control remains `off`.
11. Set 8 A, then manually start charging from the charge-control switch. Confirm the app log
    shows an accepted `RemoteStartTransaction`, then a `StartTransaction` and
    charging `StatusNotification` from the Elvi.
    Verify physical current starts near 8 A, not the previous 12 A default.
12. During the active transaction, set 6 A, 8 A, and 12 A one at a time. For
    every value, confirm an accepted transaction-bound `TxProfile`, the number
    state update, and a matching physical change in
    `sensor.charger_current_import`. Also verify that 5 A produces the existing
    suspended behavior used by the solar automation.
13. Stop charging and confirm an accepted `RemoteStopTransaction`, followed by
    zero measured current, zero power, and charge control `off`.
14. Apply the device-to-entity automation replacements above. Validate the Home
    Assistant configuration and inspect the UI-managed guarded script.
15. Enable `Safely apply current on charger`, then `Adapt charging power`, and
    finally `Auto-start charging`, in that order.
16. Supervise at least one complete solar-controlled session. Check every
    calculated setpoint against the Elvi log and the physical charging current.
17. Restart during a supervised active/suspended session, then during idle with
    a saved 8 A limit. Confirm synchronization, correct measured current after a
    fresh start, and recovery of `Adapt charging power` when enabled.

## Failure behavior

- When the wallbox disconnects, MQTT availability becomes `offline`; commands
  are rejected and retained entity states are not presented as live.
- Reconnection resets both switches, the number, and measurements to unknown.
  Connector status and transaction-bound MeterValues recover observations; the
  saved limit is reapplied before publishing the confirmed number. Session ON
  does not require accepted current or meter arrival. Suspended sessions remain
  active even at zero measured current; guard missing power in HA automations.
- Remote start includes the saved current profile and reapplies it once the
  transaction ID is assigned. RemoteStart/Stop acceptance is not completion: the
  switches follow actual session evidence. Explicit starts from Finishing and
  other statuses reach firmware after protected default reapplication. No
  unprofiled fallback or automatic start/stop is used.
- MQTT reconnection and HA birth republish Discovery and latest states before
  availability. Retained commands are ignored.
- Complete OCPP calls are serialized, send/response waits are bounded, and
  duplicate/late responses cannot terminate the receive loop. Retired sessions
  and historical closed-transaction telemetry cannot override live state.
- Measured current updates only when the Elvi sends `Current.Import`, normally
  at the configured `meter_value_interval`.
- While charging, a current value is published only after the Elvi accepts the
  transaction-bound `TxProfile`. A rejection retains the previous confirmation;
  timeout makes that limit unknown because the outcome is uncertain. Repeated
  explicit current values are forwarded. Recovery makes at most one attempt per
  scope, not retries on every incoming packet.
- After an active `TxProfile` is accepted, failure to update the best-effort
  `TxDefaultProfile` is logged but does not misreport the active command as
  failed.
- A stop command is not fabricated when no OCPP transaction ID is known. The
  app logs the failure and leaves the switch state unchanged.
- Optional NotSupported, including GetConfiguration on legacy firmware, is
  logged and skipped without gating control. The protocol uses pinned
  python-ocpp 2.0.0 with its 1.6 schemas; all runtime dependencies are pinned.
- Invalid or unsupported charge-point calls receive an OCPP error response;
  malformed JSON is ignored without terminating the server.
- The last current value accepted by the wallbox, transaction counter, and closed-session marker are
  stored under the app's `/data` volume and included in app backups.
- Corrupted saved state blocks automatic current restoration until a valid current
  is set explicitly. A missing file on a fresh installation uses the configured
  default. BootNotification also invalidates observations if the socket stays open.

These tests do not establish the cause of the historical intermittent disconnect
or certify electrical safety. Acceptance is not physical current measurement;
local/RFID starts can precede server synchronization. Network/HA/MQTT loss does
not automatically stop charging. Hardware limits and protections must remain
effective independently of this software. See the [full installation guide](../docs/HOME_ASSISTANT.md).

## Rollback

1. Disable the three charging automations.
2. Stop this app.
3. Restore the old OCPP integration version/configuration or restore the backup.
4. Confirm the old five entities and OCPP connection are available.
5. Restore the original device-based automation blocks if necessary.
6. Test one manual start, current change, and stop before enabling automation.
