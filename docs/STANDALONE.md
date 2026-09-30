# Avvio e test standalone

Questa guida avvia il bridge fuori da Home Assistant usando Docker CLI e
Colima. Docker Desktop non è richiesto né utilizzato.

L'ambiente risultante è:

```text
EVBox Elvi <-- OCPP 1.6J --> bridge <-- MQTT --> Mosquitto
```

Il bridge non contiene logica solare o di ricarica. Traduce soltanto messaggi
OCPP e comandi MQTT.

## Prerequisiti

Installa gli strumenti una sola volta:

```shell
brew install colima docker docker-buildx ruff shellcheck
```

Avvia Colima:

```shell
colima start --cpu 4 --memory 6 --disk 30
colima status
docker version
```

`colima status` deve riportare `colima is running` e runtime `docker`.

## Costruzione dell'immagine

Dalla root del repository, su Apple Silicon:

```shell
cd /Users/davide.gallina/Development/personal/ha-ocpp-server-app

docker build \
  --pull \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21-2025.11.1 \
  --build-arg BUILD_ARCH=aarch64 \
  --build-arg BUILD_VERSION=1.0.0 \
  --tag evbox-elvi-ocpp:test \
  evbox_elvi_ocpp
```

Su un Mac Intel sostituisci `aarch64` con `amd64` sia in `BUILD_FROM` sia in
`BUILD_ARCH`.

In alternativa, `./dev_scripts/verify.sh` costruisce la stessa immagine ed
esegue anche tutti i controlli descritti in [VERIFY.md](VERIFY.md).

## Avvio del broker MQTT

Crea una rete dedicata e avvia Mosquitto:

```shell
docker network create evbox-elvi-test

docker run --detach \
  --name evbox-test-mqtt \
  --network evbox-elvi-test \
  --volume "$PWD/dev_scripts/mosquitto-test.conf:/mosquitto/config/mosquitto.conf:ro" \
  eclipse-mosquitto:2
```

Il broker non viene pubblicato sulla LAN: i comandi di questa guida usano gli
strumenti `mosquitto_pub` e `mosquitto_sub` già presenti nel container.

## Avvio del bridge

Lo script `/run.sh` dell'add-on usa `bashio` e richiede il Supervisor. In
standalone bisogna quindi avviare direttamente `python3 -m app.main`.

Per la prima prova mantieni vuoto `EXPECTED_CHARGE_POINT_ID` e disabilita la
modifica automatica dei MeterValues:

```shell
docker volume create evbox-test-data

docker run --detach \
  --name evbox-test-bridge \
  --network evbox-elvi-test \
  --publish 9000:9000 \
  --volume evbox-test-data:/data \
  --env MQTT_HOST=evbox-test-mqtt \
  --env MQTT_PORT=1883 \
  --env MQTT_USERNAME= \
  --env MQTT_PASSWORD= \
  --env EXPECTED_CHARGE_POINT_ID= \
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
  evbox-elvi-ocpp:test \
  -m app.main
```

Segui i log in un altro terminale:

```shell
docker logs --follow evbox-test-bridge
```

## Collegamento della wallbox

Configura temporaneamente il server OCPP della Elvi come:

```text
ws://<IP-DEL-MAC>:9000/<CHARGE-POINT-ID>
```

Per esempio:

```text
ws://192.168.1.20:9000/EVB-P123
```

L'ID è il segmento finale dell'URL. Per trovare l'indirizzo Wi-Fi del Mac:

```shell
ipconfig getifaddr en0
```

Il log del bridge deve mostrare la connessione, il `BootNotification` e la
risposta `Accepted`. Dopo avere confermato l'ID, ricrea il container impostando
`EXPECTED_CHARGE_POINT_ID` allo stesso valore. Questo filtra client con un ID
diverso, ma non è autenticazione: non pubblicare mai la porta 9000 su Internet.

## Stati da osservare

Per osservare tutti i topic runtime:

```shell
docker exec -it evbox-test-mqtt \
  mosquitto_sub -h localhost -v -t 'evbox_elvi/#'
```

Gli stati principali sono:

| Topic | Significato |
| --- | --- |
| `evbox_elvi/availability` | `online` dopo un `BootNotification` accettato |
| `evbox_elvi/charge_control/state` | Stato della sessione di ricarica |
| `evbox_elvi/charger_availability/state` | `ON` se il connettore è libero, `OFF` se è occupato o non disponibile |
| `evbox_elvi/maximum_current/state` | Ultimo limite di corrente accettato dalla Elvi |
| `evbox_elvi/power_active_import/state` | Potenza di ricarica misurata, in kW |

Per osservare esplicitamente limite e potenza:

```shell
docker exec -it evbox-test-mqtt mosquitto_sub -h localhost -v \
  -t evbox_elvi/maximum_current/state \
  -t evbox_elvi/power_active_import/state
```

Il numero rappresenta il limite comandato e accettato; non è la corrente
istantanea misurata. La potenza è letta da `Power.Active.Import`, oppure
calcolata da corrente e tensione quando tale measurand manca.

La versione 1.0.0 non pubblica ancora sensori separati per corrente misurata in
ampere, energia cumulativa o energia di sessione. Non confondere quindi
`maximum_current/state` con l'amperaggio istantaneo.

Per osservare i messaggi MQTT Discovery:

```shell
docker exec -it evbox-test-mqtt \
  mosquitto_sub -h localhost -v -t 'homeassistant/#'
```

## Comandi disponibili

Questi sono tutti i comandi MQTT pubblici implementati:

| Topic | Payload | Effetto OCPP |
| --- | --- | --- |
| `evbox_elvi/charge_control/set` | `ON` | `RemoteStartTransaction` |
| `evbox_elvi/charge_control/set` | `OFF` | `RemoteStopTransaction` |
| `evbox_elvi/charger_availability/set` | `ON` | `ChangeAvailability: Operative` |
| `evbox_elvi/charger_availability/set` | `OFF` | `ChangeAvailability: Inoperative` |
| `evbox_elvi/maximum_current/set` | numero | `SetChargingProfile: TxDefaultProfile` |

Il limite deve essere finito e compreso tra 0 e `MAXIMUM_CURRENT`. La Elvi può
rifiutare valori che il suo firmware non supporta. Il valore 5 A è consentito
per preservare il comportamento di sospensione usato dall'automazione solare.

### Regolazione della corrente

Apri prima il subscriber degli stati, poi prova in ordine 6, 8 e 12 A:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 6

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 8

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 12
```

Verifica per ogni comando:

1. `SetChargingProfile` accettato nel log;
2. aggiornamento di `maximum_current/state`;
3. corrente e potenza fisiche coerenti.

Prova 5 A solo dopo i valori normali:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/maximum_current/set -m 5
```

La prova deve mostrare il comportamento di sospensione già usato in Home
Assistant e una potenza che scende a zero.

Se la potenza non viene ricevuta perché la Elvi non ha già configurato i
MeterValues richiesti, ricrea il bridge con
`CONFIGURE_METER_VALUES=true`. Il bridge legge prima la configurazione e cambia
soltanto le chiavi diverse e non read-only.

### Avvio e arresto

Con il veicolo collegato:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charge_control/set -m ON
```

Attendi `StartTransaction`, quindi arresta:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charge_control/set -m OFF
```

Lo stop viene rifiutato se il bridge non ha ancora ricevuto un transaction ID
dalla wallbox.

### Disponibilità

Usa questi comandi solo dopo aver validato start, stop e corrente:

```shell
docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charger_availability/set -m OFF

docker exec evbox-test-mqtt mosquitto_pub -h localhost \
  -t evbox_elvi/charger_availability/set -m ON
```

`OFF` rende il connettore `Inoperative`; `ON` lo riporta `Operative`.

## Sequenza di prova raccomandata

1. Avvia broker e bridge senza veicolo.
2. Collega la Elvi e verifica `BootNotification`.
3. Controlla availability `ON`, charge control `OFF` e potenza `0.000`.
4. Collega il veicolo e verifica availability `OFF`.
5. Avvia la sessione.
6. Prova 6, 8 e 12 A verificando log, stati MQTT e comportamento fisico.
7. Prova 5 A e conferma la sospensione.
8. Ripristina 12 A, quindi arresta la sessione.
9. Verifica charge control `OFF` e potenza `0.000`.

## Arresto e pulizia

```shell
docker stop evbox-test-bridge evbox-test-mqtt
docker rm evbox-test-bridge evbox-test-mqtt
docker network rm evbox-elvi-test
```

Il volume `evbox-test-data` viene conservato intenzionalmente. Per eliminare
anche corrente persistita e contatore delle transazioni:

```shell
docker volume rm evbox-test-data
```

Quest'ultimo comando cancella lo stato del laboratorio e non è necessario per
le esecuzioni successive.
