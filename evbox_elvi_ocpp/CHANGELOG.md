# Changelog

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
