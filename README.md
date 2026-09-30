# EVBox Elvi OCPP bridge

This repository contains one deliberately small Home Assistant app: an OCPP
1.6J central system for a single EVBox Elvi, exposed to Home Assistant through
MQTT Discovery.

The app contains no charging, solar, scheduling, or load-balancing logic. Home
Assistant remains responsible for all automation decisions.

See [the app documentation](evbox_elvi_ocpp/DOCS.md) for installation,
migration, and validation instructions.

## Development checks

The protocol tests use only the Python standard library:

```shell
PYTHONPATH=evbox_elvi_ocpp python3 -m unittest discover -s tests -v
ruff check .
```

With Docker and Colima running, the complete image build, in-container tests,
temporary MQTT broker, and OCPP/MQTT smoke test are run with:

```shell
./dev_scripts/verify.sh
```
