# Changelog

## 1.4.0

- Replace the handwritten OCPP transport with pinned python-ocpp 2.0.0 (1.6
  routing, schemas, serialization and response correlation), retaining bounded
  waits, disconnect cancellation, duplicate replay and late-response filtering.
- Replace scattered session/readiness flags with one connection-scoped model.
  Session, connector, measurements, active-profile and default-profile acceptance
  are distinct facts; a persisted recovery target is not a live acknowledgment.
- Forward explicit starts from Finishing and other statuses to the firmware;
  reassert the protected default and embed the saved limit in remote start.
  Reapply a transaction-bound limit immediately after assigning a transaction ID.
- Recover active/suspended sessions without BootNotification. Perform bounded
  limit restoration once per scope; optional NotSupported does not gate commands.
- Invalidate uncertain limits on timeout; retain confirmed limits on rejection.
  Forward repeated explicit commands as retries, never automatically start/stop.
- Update regression/mock coverage and document mandatory automation guards for
  missing data. Keep all five MQTT IDs and do not edit the HA repository.
- This version requires container and supervised Elvi validation before deployment;
  software tests do not certify electrical safety or diagnose historical disconnects.

## 1.3.0

- Centralize connection-state reset and connector switch reconciliation. Clear
  stale measurements to unknown, preserving the saved requested current limit.
- Keep switches unknown until charger status or transaction data is received.
- Recover charge control and the active transaction from transaction-bound
  MeterValues after reconnection, including suspended sessions with zero current.
- Request connector status after the first OCPP call, even without BootNotification.
- Include the last accepted current limit as a TxProfile in RemoteStartTransaction;
  do not fall back to starting without a profile when rejected.
- Add regression and OCPP/MQTT smoke coverage for profiled starts and recovery.
- Reapply the saved current on recovered sessions before publishing confirmed
  limit/session states; never acknowledge an active limit using only a default.
- Serialize complete OCPP calls, bound send/response waits, ignore late/duplicate
  responses, and replay recent duplicate incoming calls without repeating effects.
- Ignore retired-connection responses and closed/other-transaction telemetry.
- Publish session start/stop from actual events, not RemoteStart/Stop acceptance;
  bound pending-command suppression to allow explicit retries.
- Restore retained states and availability after MQTT reconnection or HA birth;
  ignore retained control commands.
- Validate finite limits and persisted state, flush atomic state writes, and
  avoid counting aggregate and phase power twice.
- Block automatic restoration of corrupted persisted limits and discard pre-boot
  acknowledgments even when a wallbox reboots without changing its socket.
- Document recovery-aware HA triggers, hardware limitations, and supervised tests.

## 1.2.1

- Publish MQTT availability as online when an OCPP connection is accepted,
  including reconnections without BootNotification. Publish offline when the
  active connection closes.

## 1.2.0

- Apply current changes to an active transaction with a transaction-bound
  `TxProfile` instead of only updating the next transaction's default.
- Keep `TxDefaultProfile` synchronized as a best-effort follow-up so the final
  limit remains the default for the next charging session.
- Extend the OCPP/MQTT smoke test to cover a dynamic current change during an
  active transaction.
- Log invalid or charger-rejected MQTT commands as concise warnings while
  preserving error tracebacks for unexpected failures.

## 1.1.0

- Publish measured `Current.Import` through MQTT Discovery as
  `sensor.charger_current_import`.
- Average active per-phase current samples when the Elvi doesn't provide an
  aggregate current value.

## 1.0.0

- Add the OCPP 1.6J WebSocket central system for one EVBox Elvi.
- Expose the existing Home Assistant charger entity IDs through MQTT Discovery.
- Implement remote start, remote stop, availability, and current-limit commands.
- Publish charging power from OCPP meter values.
