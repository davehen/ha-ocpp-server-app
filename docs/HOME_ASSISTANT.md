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
| `sensor.charger_current_import` | Instantaneous measured charging current in A |
| `sensor.charger_power_active_import` | Instantaneous charging power in kW |

The existing dashboard and charging automations use these preserved IDs.
The October 8 registry export already shows the five MQTT entities; an exported
snapshot is not a check of the live installation. The active YAML does not
currently reference the measured-current sensor.

The number holds the commanded limit, while `sensor.charger_current_import`
comes from the latest OCPP `Current.Import` sample. An unphased sample is used
directly; when the Elvi reports individual phases, the sensor is the average of
the active phases. The power sensor uses `Power.Active.Import`, or falls back to
current, voltage, and the configured number of phases.

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
five names above, but they cannot inherit the internal IDs belonging to the
removed OCPP integration.

The original migration affected four blocks (the current October 8 YAML
already contains these state/action replacements):

- the `Adapt charging power` trigger;
- two `Auto-start charging` conditions;
- the `Auto-start charging` turn-on action.

`Safely apply current on charger` and `lovelace/vehicles_card.yaml` already use
the visible entity IDs. No HA repository file is modified by this project.
The missing-data guards below are still needed before enabling automation.

### Adapt charging power trigger

Use a state trigger that also accepts `unknown`/`unavailable` → `on`, plus a
Home Assistant startup trigger guarded by the actual session state. Replace the
trigger list and add this condition; keep the existing actions and `mode: single`:

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

Do not add `from: "off"`: that would miss a recovered session. The
`switch.turned_on` target trigger is not the recovery-aware replacement. This is
also needed when the device-trigger migration has already been completed in the
UI. The server cannot restart a stopped HA automation without a suitable trigger;
it deliberately does not fabricate an OFF/ON cycle.

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

## Automation data safety

### Mandatory missing-data guards in Adapt charging power

In 1.4.0, charge control ON means an **observed session**, not "all measurements
and limits are initialized". The current YAML converts missing charger/home power
to zero using `float(0)`, and missing confirmed current to 6 A. Those are not
observations suitable for solar calculations. Preserve existing solar math,
thresholds, delay, termination and cleanup policy; change only data handling:

1. Inside `repeat.sequence`, wrap **Calculate new amperage** and the subsequent
   **If target differs** action in this conditional. Keep the existing timeout
   delay outside it, so missing data skips calculations but the watchdog keeps
   running and can recover:

   ```yaml
   - alias: Calculate and apply only with valid live power inputs
     if:
       - condition: template
         value_template: >-
           {{ is_state('switch.charger_charge_control', 'on')
              and is_number(states('sensor.charger_power_active_import'))
              and is_number(states('sensor.power_meter_active_power')) }}
     then:
       # Put the existing Calculate new amperage variables action here.
       # Put the existing If target differs / set-current event action here.
   ```

2. In the variables action, replace these definitions:

   ```yaml
   current_amp: "{{ states('number.charger_maximum_current') | float(none) }}"
   need_update: >-
     {{ current_amp is none or (target_amp - current_amp) | abs > TOLERANCE_AMP }}
   ```

   An unknown limit now requests an explicit retry of the calculated target
   instead of inventing 6 A or waiting forever for a failed recovery ACK.
   Power is calculated only from valid inputs; zero from an actual suspension
   remains valid. Before transaction-ID recovery, the bridge can still reject
   the command; the watchdog retries through the existing setting automation.

The guard uses Home Assistant's documented
[`is_number`](https://www.home-assistant.io/template-functions/is_number/)
finite-number check rather than substituting a numeric value for missing data.

These are manual HA changes, not hidden charging logic in the add-on. Skipping
an automation calculation does **not** stop charging: the last firmware limit
can continue. Lost-data behavior remains an HA policy decision. Numeric retained
readings are not a guarantee of freshness; inspect telemetry timestamps/logs if
communication stalls. The bridge does not certify installation protection.

The existing `FINAL_AMP: 12` is unchanged. If that cleanup was accepted, 12 A
is the saved next-start target. Setting 8 A before start means 8 A is protected;
the bridge does not silently choose 8 A when HA last requested 12 A.

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
6. Stop and remove the old OCPP integration config entry so the five entity IDs
   become free.
7. Confirm that no other process is using TCP port 9000.
8. Start the add-on.
9. Keep this URL on the Elvi:

   ```text
   ws://<HOME-ASSISTANT-IP>:9000/<CHARGE-POINT-ID>
   ```

10. Check the log for the accepted OCPP connection and verify that
    `evbox_elvi/availability` is `online`. The Elvi may resume with `Heartbeat`
    or `MeterValues` without a new `BootNotification`.
11. Copy the ID from the log into `expected_charge_point_id`, save, and restart
    the add-on.
12. Confirm that the five entities were created without numeric suffixes.

A name such as `sensor.charger_power_active_import_2` means an old entity still
owns the required ID. Do not continue until that conflict has been resolved.

## Functional validation

Before deploying 1.4.0, complete container/mock verification; keep automatic
updates off. When updating an existing installation, update and restart the add-on,
then wait for the Elvi to reconnect. Verify that `evbox_elvi/availability` is
`online`. Switches and the limit may initially be `unknown`: wait for connector
recovery and the logged accepted current profile. No entity recreation or MQTT
broker changes are required. Apply the recovery-aware trigger and mandatory
missing-data guards above before enabling charging automation.

Run this test under direct supervision.

1. Without a vehicle, verify availability `on`, charge control `off`, and power
   `0 kW`.
2. Connect the vehicle and verify availability `off` and charge control `off`.
3. Set 8 A before starting, then manually turn on `switch.charger_charge_control`.
4. Check the log for `RemoteStartTransaction`, `StartTransaction`, and a
   charging `StatusNotification`.
   Confirm measured current settles near 8 A, not the previous 12 A limit.
   Acceptance of the profile alone does not prove the physical limit was applied.
5. During the active transaction, set 6 A, 8 A, and 12 A. For each value,
   confirm:
   - an accepted `TxProfile` containing the active transaction ID;
   - an updated `number.charger_maximum_current`;
   - a corresponding physical change in `sensor.charger_current_import`;
   - an updated `sensor.charger_power_active_import`.
6. Set 5 A and confirm the suspension behavior used by the solar automation and
   measured current and power falling to zero.
7. Restore 12 A.
8. Stop the session and verify `RemoteStopTransaction`, then the actual
   `StopTransaction`, charge control `off`, and power at zero. Acceptance alone
   must not immediately flip the session switch.
9. During a supervised 8 A session, restart the add-on. Confirm reconnection,
   charge control returning to `on`, and measured current remaining near 8 A.
   Repeat with a suspended session: zero current must not imply a closed session.
10. With an idle car and saved 8 A, restart the add-on, wait for synchronization,
    and start a fresh session. Check the initial active profile and measured
    current: it must not silently use the previous 12 A default.
11. After manual validation, enable automation and repeat a supervised restart
    during charging. Confirm `Adapt charging power` is running again after the
    recovered switch becomes `on`.

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
- After connection, switches and the number are unknown until connector/session
  evidence and current synchronization respectively are available. Transaction-bound
  MeterValues recover an active or suspended session; ON does not require accepted
  current or power arrival, and does not mean automation inputs are ready.
  Connector status is requested after the
  first OCPP call, without requiring BootNotification.
- A single connection reset invalidates both switches, the transaction ID, and
  measured current/power before publishing online. Missing measurements use the
  MQTT `None` payload (Home Assistant `unknown`), not a guessed zero. Explicit
  non-charging connector status or StopTransaction may still publish zero.
- Connector availability is reconciled with session evidence: StartTransaction
  establishes an occupied connector; StopTransaction does not prove the cable
  was unplugged. Until new connector status arrives, availability is unknown.
- The saved setpoint is not a readback. On recovery the bridge reapplies it as an
  active TxProfile or idle TxDefaultProfile before publishing the confirmed number.
  An active session without a transaction ID cannot acknowledge a default-only
  update as its active limit. A failed recovery leaves the number unknown and
  does not retry on every normal message; explicitly retry the current command.
  There is no automatic start/stop or solar policy.
- Remote start includes the last accepted current as a TxProfile. Rejection does
  not trigger a second start without a limit. Confirm the measured current on
  real hardware; an accepted OCPP response is not a measurement.
- These changes remove reproduced server-side weaknesses but do not establish
  the cause of the historical disconnection. Keep automations disabled during
  initial hardware validation and retain DEBUG logs if it recurs.
- Measured current is published only when the Elvi sends `Current.Import`; its
  normal update cadence is therefore `meter_value_interval`.
- During charging, a new current limit is published only after the Elvi accepts
  a `TxProfile` bound to the active transaction ID.
- A rejected active profile preserves the previous confirmation. Timeout makes
  the affected limit unknown (uncertain outcome), retaining the saved target and
  observed session. Repeated explicit commands are forwarded.
- The app then updates `TxDefaultProfile` as a best-effort default for the next
  transaction; failure of this follow-up is logged without undoing the active
  limit.
- A stop is rejected when no transaction ID is known.
- RemoteStart/Stop acceptance is not session completion. No local status whitelist
  vetoes explicit start, including Finishing. Start reasserts the protected default
  and includes its profile; failure never falls back to an unprofiled start.
  Explicit repeated commands reach firmware; no automatic start/stop is added.
- Unsupported optional GetConfiguration/TriggerMessage operations are logged and
  skipped. They never gate session state or prevent subsequent commands.
- Calls are serialized and send/response waits are bounded. Duplicate/late
  responses and retired connections cannot overwrite the current session.
- Closed/other-transaction telemetry and older timestamped observations are ignored.
- After broker reconnection or HA birth, Discovery and latest states are republished
  before online/offline. Retained control commands are ignored.
- An unsupported OCPP action receives a `CALLERROR`.
- Malformed JSON is ignored without stopping the server.
- The last accepted current, transaction counter, and closed-transaction marker are saved under `/data` and
  included in add-on backups.
- Invalid/corrupted saved state is logged and blocks automatic current restoration;
  set a valid current explicitly to resume synchronization. A missing file on a
  fresh installation uses the configured initial default. No corrupted setpoint
  silently becomes an automatic increase to the configured ceiling.

An OCPP Accepted response is not proof of the physical current limit. Local/RFID
starts can begin before the server receives the transaction ID and reapplies its
profile. MQTT/HA/network loss does not automatically stop charging: the Elvi may
keep its last applied limit. The configured ceiling is software validation, not
an electrical protection. Hardware installation limits and protections must remain
effective independently of this add-on. Tests do not certify electrical safety.

## Rollback

1. Disable the three charging automations.
2. Stop the add-on.
3. Restore the old integration and its configuration, or restore the complete
   backup.
4. Confirm that the five original OCPP entities return.
5. Restore the original device-based blocks if necessary.
6. Manually test start, current adjustment, and stop.
7. Re-enable automations only after the manual test succeeds.

## Related guides

- [Standalone server and manual testing](STANDALONE.md)
- [Automated build, mock, and tests](VERIFY.md)
