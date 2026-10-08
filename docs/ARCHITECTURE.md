# Frozen bridge contract

## Responsibilities

`ocpp.py` adapts the pinned python-ocpp 2.0.0 **1.6** implementation. The library
owns schemas, camel/snake serialization, routing, CALLERROR and correlation.
The small adapter owns socket lifetime, bounded send/response waits, a 32-entry
duplicate incoming-CALL replay cache, and filtering late/duplicate replies.
It does not implement another JSON protocol engine. Its library APIs and
dependency graph are pinned and exercised by wire tests.

`model.py` owns live connector/session observations and separately accepted
active/default limits. `bridge.py` reduces incoming events and runs serialized
outgoing effects. `mqtt.py` publishes the five existing entities. `state.py`
persists only the last accepted recovery target and transaction counters.

Home Assistant owns every solar, scheduling and charging-policy decision.
No automatic RemoteStartTransaction, RemoteStopTransaction, Reset, charger
reboot, or stop/start substitute is introduced.

## Facts, targets and readiness

| Information | Evidence | What it does NOT mean |
| --- | --- | --- |
| Session ON | StartTransaction, active/suspended status, or transaction-bound MeterValues | Current is flowing, a profile was accepted, or telemetry is ready |
| Session OFF | Idle status or matching StopTransaction | Cable was unplugged |
| Availability ON/OFF | Free/occupied connector evidence | An independent physical plug sensor or operational readback |
| Confirmed number while active | Accepted TxProfile bound to the current transaction | Physically measured current |
| Confirmed number while idle | Accepted TxDefaultProfile on this connection | Guaranteed initial current on an untested firmware |
| Saved recovery target | Last accepted limit, atomically saved | A live acknowledgment after reboot/reconnection |
| Current/power | Latest valid OCPP measurement (or documented AC power fallback) | A setpoint or zero when data is missing |

Faulted/Unavailable/Reserved produce unknown availability, not a false "plugged
idle" signal for Auto-start. A suspended session remains ON at zero measured
current. StartTransaction marks occupancy but cannot invent telemetry. A return
from suspension to Charging invalidates the previous zero until new meters arrive.

Connection replacement, disconnect and BootNotification reset all live facts.
A `(connection/boot epoch, session generation, transaction ID)` scope makes
old acknowledgments and queued commands unable to modify a new session.
Measurements older than the latest status/meter are ignored. Closed transaction
IDs are not resurrected; another transaction's data cannot replace an active ID.

The MQTT snapshot publishes measurements and limit before session switches;
this is ordering, **not atomicity across MQTT topics**. HA must guard its inputs.

## Commands and recovery

An explicit start reasserts the last accepted default and sends a remote start
with a TxProfile. Firmware decides whether the status permits start, including
Finishing. A failed protected default prevents that start rather than falling
back to an unprofiled command. Acceptance does not fabricate ON. A fresh
StartTransaction is acknowledged before a background bound TxProfile is sent.
Local/RFID starts may draw current before this exchange; independent hardware
limits remain essential.

An explicit stop requires a recovered transaction ID. Acceptance does not
fabricate OFF. Availability commands similarly wait for observed connector state.
All explicit valid commands, including repeated values, can reach the firmware;
there is no indefinite local pending/status veto or silent same-value no-op.

Current commands validate finite 0..configured ceiling values. Active TxProfile
is applied first; its accepted target is persisted/published before a best-effort
default update. Idle commands apply a default. An observed active session without
an ID cannot claim a default as an active limit. Completely unknown session state
can accept an explicit default but cannot expose an active/idle confirmed number.

Rejected profiles preserve the previous confirmation. Timeout invalidates the
affected confirmation (unknown outcome), not the observed session or saved target.
Late replies are ignored; no blind command retry follows a timeout. Disconnect
cancels pending calls, not the entire listener. Expected communication failures
are concise warnings; unexpected programming/storage faults retain tracebacks.

Recovery restores the trusted target at most once per scope, including recovered
zero-current suspended sessions. Normal traffic cannot cause a retry storm.
An explicit current command can retry after a failed restoration. Optional
TriggerMessage/GetConfiguration/ChangeConfiguration operations are bounded,
release the lock individually and never gate start or state publication.
NotSupported is logged and skipped, not treated as a server crash. On a failed
recovery, wait for inspection/manual retry rather than inventing a confirmed limit.

## Compatibility reference and validation limits

Davide identified **lbbrhzn/ocpp 0.7.0** as the last working integration. This is
the reference version for any comparison, not the current upstream branch.
Its tagged sources were not retrievable in the current tool environment, so
the selected python-ocpp version is **not claimed to match that release's
dependency pin**. The 1.6 wire contract and the actual supplied Elvi exchanges
are the implemented reference; the 0.7.0 source comparison remains outstanding.

The earlier controller tests were rewritten against this contract rather than
private readiness/pending flags. Coverage retains profiles, meters, finite limits,
rejection, persistence, scopes, historical telemetry, replay, timeouts, broker
recovery and IDs. Old assertions that ON required accepted current/power, that
Finishing vetoed start, that same-value commands disappeared, or that every
message retried recovery were deliberately replaced with the opposite invariants.
Wire fixtures now contain all required OCPP fields; schemas are not disabled
to accommodate previously invalid empty fixture payloads.

Container/mock checks and supervised real Elvi checks are separate release
gates. Neither proves physical enforcement or electrical safety. None of these
changes establishes the cause of the historical intermittent disconnection.
DEBUG logs include full protocol payloads and may contain RFID identifiers:
redact them before sharing.
