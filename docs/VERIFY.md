# Build, mock e test automatici

`dev_scripts/verify.sh` esegue la verifica completa senza Home Assistant e
senza collegare una wallbox reale.

## Prerequisiti

Su macOS usa Docker CLI con Colima; Docker Desktop non è richiesto:

```shell
brew install colima docker docker-buildx ruff shellcheck
colima start --cpu 4 --memory 6 --disk 30
```

Controlla l'ambiente:

```shell
colima status
docker version
docker buildx version
ruff --version
shellcheck --version
```

Lo script supporta host `arm64` e `x86_64` e seleziona automaticamente
l'immagine Home Assistant `aarch64` o `amd64` corrispondente.

## Esecuzione completa

Dalla root del repository:

```shell
cd /Users/davide.gallina/Development/personal/ha-ocpp-server-app
./dev_scripts/verify.sh
```

La verifica termina correttamente soltanto quando stampa:

```text
Container smoke test passed
All container checks passed
```

## Cosa verifica

Lo script esegue, nell'ordine:

1. `ruff check .` su applicazione, test e mock;
2. ShellCheck su `run.sh` e sullo script stesso;
3. build reale dell'immagine con Python 3.13 e base Home Assistant fissata;
4. tutti i test `unittest` dentro l'immagine appena costruita;
5. creazione di una rete Docker temporanea;
6. avvio di un broker Mosquitto anonimo confinato alla rete temporanea;
7. avvio del bridge con `CONFIGURE_METER_VALUES=false`;
8. esecuzione di un charge point OCPP 1.6J simulato;
9. cleanup automatico di container e rete, anche in caso di errore.

La build usa `--pull`, quindi verifica anche che la base fissata sia ancora
reperibile dal registry.

## Cosa fa il mock

`dev_scripts/smoke_client.py` simula una wallbox con ID `EVB-P123` e verifica
end-to-end:

- handshake WebSocket con subprotocollo `ocpp1.6`;
- `BootNotification` e risposta `Accepted`;
- eventuale `TriggerMessage` iniziale;
- pubblicazione MQTT del limite a 5 A;
- ricezione OCPP di `SetChargingProfile`;
- uso di `TxDefaultProfile` e limite `5.0`;
- pubblicazione del limite accettato su
  `evbox_elvi/maximum_current/state`;
- invio di `MeterValues` con `Power.Active.Import = 2300 W`;
- pubblicazione di `2.300` su
  `evbox_elvi/power_active_import/state`.

I test unitari coprono inoltre start/stop remoto, transaction ID, stati del
connettore, sospensione, rifiuto dei profili, fallback corrente/tensione,
correlazione delle risposte OCPP, configurazione MeterValues e conservazione
degli entity ID richiesti.

Il mock non comunica con la Elvi reale, non modifica Home Assistant e non
espone porte sulla LAN.

## Risorse create e cleanup

Ogni esecuzione usa nomi contenenti il PID dello script:

```text
ha-ocpp-verify-<pid>
ha-ocpp-verify-mqtt-<pid>
ha-ocpp-verify-bridge-<pid>
```

La funzione di cleanup rimuove esclusivamente quei due container e quella rete.
L'immagine `evbox-elvi-ocpp:test` viene mantenuta per i test standalone. Non
vengono creati volumi persistenti dal mock.

## Controlli singoli

Test unitari senza container:

```shell
PYTHONPATH=evbox_elvi_ocpp python3 -m unittest discover -s tests -v
```

Lint Python:

```shell
ruff check .
```

Lint shell:

```shell
shellcheck evbox_elvi_ocpp/run.sh dev_scripts/verify.sh
```

Validazione sintattica shell:

```shell
sh -n evbox_elvi_ocpp/run.sh
bash -n dev_scripts/verify.sh
```

## Diagnostica degli errori

### Colima non è attivo

```shell
colima start --cpu 4 --memory 6 --disk 30
colima status
```

### Il daemon Docker non risponde

```shell
docker context ls
docker version
```

Il contesto attivo deve raggiungere il socket indicato da `colima status`.

### La build non scarica la base

Controlla proxy/VPN aziendale e prova:

```shell
docker pull ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21-2025.11.1
```

Su host Intel usa l'immagine `amd64-base-python` equivalente.

### Il mock fallisce

Lo script stampa i log del bridge quando il server non diventa pronto. Per
ispezionare eventuali risorse rimaste dopo un'interruzione forzata:

```shell
docker ps --all --filter name=ha-ocpp-verify
docker network ls --filter name=ha-ocpp-verify
```

Non rimuovere risorse con nomi diversi: non appartengono a questa verifica.
