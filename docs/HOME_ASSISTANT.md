# Installazione e migrazione su Home Assistant

Questa è la procedura canonica per sostituire l'integrazione custom OCPP con
l'add-on EVBox Elvi OCPP bridge.

Il percorso dati finale è:

```text
EVBox Elvi <-- OCPP 1.6J --> add-on <-- MQTT --> Home Assistant
```

L'add-on non contiene logica solare, scheduling o load balancing. Le decisioni
rimangono nelle automazioni Home Assistant.

## Prerequisiti

- Home Assistant OS o una installazione che supporti gli add-on;
- broker MQTT configurato attraverso il Supervisor;
- integrazione MQTT attiva in Home Assistant con Discovery abilitato;
- backup completo recente;
- accesso alla configurazione OCPP della Elvi;
- nessun veicolo in carica durante il cutover.

Non possono esistere due server OCPP contemporaneamente sulla porta TCP 9000.

## Entità create

MQTT Discovery richiede esattamente questi entity ID:

| Entity ID | Uso |
| --- | --- |
| `switch.charger_charge_control` | Avvio e arresto remoto della sessione |
| `switch.charger_availability` | Stato libero/occupato e disponibilità operativa |
| `number.charger_maximum_current` | Limite di corrente accettato dalla Elvi |
| `sensor.charger_power_active_import` | Potenza istantanea di ricarica in kW |

La dashboard `lovelace/vehicles_card.yaml`, l'automazione `Safely apply current
on charger` e la logica di `Adapt charging power` usano questi nomi.

La number contiene il limite comandato, non la corrente istantanea misurata. Il
sensore power usa `Power.Active.Import` o, se assente, un fallback basato su
corrente, tensione e numero di fasi.

## Aggiunta del repository

Nella sezione add-on di Home Assistant:

1. apri lo store;
2. apri il menu dei repository;
3. aggiungi:

```text
https://github.com/davehen/ha-ocpp-server-app
```

4. aggiorna lo store;
5. installa `EVBox Elvi OCPP bridge`;
6. non avviarlo ancora.

Lascia gli aggiornamenti automatici disabilitati. L'obiettivo del progetto è
mantenere un server OCPP 1.6J stabile una volta validato.

## Configurazione iniziale

I valori predefiniti sono:

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

Mantieni la mappatura TCP `9000` su `9000`.

- Lascia vuoto `expected_charge_point_id` al primo avvio. Copia poi dal log
  l'ID rilevato e salvalo nell'opzione.
- `id_tag` deve contenere da 1 a 20 caratteri ed è usato nel remote start.
- `configure_meter_values` legge prima la configurazione della Elvi e modifica
  soltanto valori diversi e scrivibili.
- `maximum_current` è il tetto dei comandi MQTT e dello slider, non sostituisce
  il limite elettrico dell'impianto.
- `number_of_phases` viene usato soltanto per il calcolo di fallback della
  potenza.

Il charge point ID filtra l'URL del client, ma non è autenticazione. La porta
9000 deve rimanere sulla LAN fidata e non deve essere inoltrata da Internet.

## Limite delle automazioni device-based

Home Assistant salva nelle automazioni device-based gli ID interni del registro
entità, non soltanto l'`entity_id` visibile. Le nuove entità MQTT possono
riutilizzare i quattro nomi sopra, ma non possono ereditare gli ID interni
dell'integrazione OCPP rimossa.

L'ispezione statica di `davehomeassistant` ha individuato quattro blocchi da
sostituire:

- il trigger di `Adapt charging power`;
- due condizioni di `Auto-start charging`;
- l'azione di accensione di `Auto-start charging`.

`Safely apply current on charger` e `lovelace/vehicles_card.yaml` usano già gli
entity ID visibili e non richiedono modifiche.

### Trigger di Adapt charging power

Sostituisci il trigger device-based con:

```yaml
- alias: When charging starts
  trigger: state
  entity_id: switch.charger_charge_control
  from: "off"
  to: "on"
```

### Condizioni di Auto-start charging

Sostituisci le due condizioni device-based con:

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

### Azione di Auto-start charging

Sostituisci l'azione device-based con:

```yaml
- action: switch.turn_on
  target:
    entity_id: switch.charger_charge_control
```

Non applicare queste modifiche finché le nuove entità MQTT non esistono con gli
ID esatti.

## Risorse gestite dalla UI da controllare

L'export del registro contiene anche:

- `script.charger_charge_control_guarded`;
- `binary_sensor.car_is_charging`.

Le relative definizioni non sono presenti nel repository. Prima del cutover,
aprile nella UI e verifica che usino gli entity ID visibili. Sostituisci
eventuali azioni device-based con azioni per entità.

## Procedura sicura di cutover

1. Assicurati che nessun veicolo stia caricando.
2. Crea un backup completo di Home Assistant.
3. Registra la versione e la configurazione dell'integrazione OCPP attuale.
4. Disabilita temporaneamente `Safely apply current on charger`, `Adapt
   charging power` e `Auto-start charging`.
5. Installa e configura l'add-on senza avviarlo.
6. Arresta e rimuovi la config entry della vecchia integrazione OCPP affinché i
   quattro entity ID diventino liberi.
7. Verifica che nessun altro processo occupi TCP 9000.
8. Avvia l'add-on.
9. Mantieni sulla Elvi l'URL:

   ```text
   ws://<IP-HOME-ASSISTANT>:9000/<CHARGE-POINT-ID>
   ```

10. Controlla nel log connessione, `BootNotification` e risposta `Accepted`.
11. Copia l'ID dal log in `expected_charge_point_id`, salva e riavvia l'add-on.
12. Verifica che le quattro entità siano state create senza suffissi numerici.

Un nome come `sensor.charger_power_active_import_2` indica che una vecchia
entità possiede ancora l'ID richiesto. Non proseguire finché il conflitto non è
stato risolto.

## Validazione funzionale

Esegui la prova sotto supervisione diretta.

1. Senza veicolo, verifica availability `on`, charge control `off` e power
   `0 kW`.
2. Collega il veicolo e verifica availability `off` e charge control `off`.
3. Avvia manualmente `switch.charger_charge_control`.
4. Controlla nel log `RemoteStartTransaction`, `StartTransaction` e
   `StatusNotification` di charging.
5. Imposta 6 A, 8 A e 12 A. Per ogni valore controlla:
   - `SetChargingProfile` accettato;
   - aggiornamento di `number.charger_maximum_current`;
   - corrente fisica coerente;
   - aggiornamento di `sensor.charger_power_active_import`.
6. Imposta 5 A e conferma la sospensione usata dall'automazione solare e power
   a zero.
7. Ripristina 12 A.
8. Arresta la sessione e verifica `RemoteStopTransaction`, charge control
   `off` e power a zero.

Solo dopo questa prova applica i blocchi YAML sostitutivi.

## Riattivazione delle automazioni

Riattiva in questo ordine:

1. `Safely apply current on charger`;
2. `Adapt charging power`;
3. `Auto-start charging`.

Supervisiona almeno una sessione solare completa. Confronta ogni setpoint con
il log dell'add-on, lo stato della number e la corrente fisica della Elvi.

## Comportamento in caso di errore

- Se la wallbox si disconnette, le entità MQTT diventano unavailable.
- Un nuovo limite viene pubblicato soltanto dopo che la Elvi accetta
  `SetChargingProfile`.
- Un rifiuto o timeout conserva il valore precedente, permettendo al watchdog
  dell'automazione di riprovare.
- Lo stop viene rifiutato se non è noto un transaction ID.
- Un'azione OCPP non supportata riceve un `CALLERROR`.
- JSON malformato viene ignorato senza arrestare il server.
- Ultimo limite accettato e contatore delle transazioni sono salvati in `/data`
  e inclusi nel backup dell'add-on.

## Rollback

1. Disabilita le tre automazioni di ricarica.
2. Arresta l'add-on.
3. Ripristina la vecchia integrazione e la sua configurazione, oppure il backup
   completo.
4. Verifica il ritorno delle quattro entità OCPP originali.
5. Ripristina i blocchi device-based originali, se necessario.
6. Prova manualmente start, cambio corrente e stop.
7. Riattiva le automazioni soltanto dopo il test manuale.

## Guide correlate

- [Avvio e test standalone](STANDALONE.md)
- [Build, mock e test automatici](VERIFY.md)
