# Changelog

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
