# EVBox Elvi OCPP bridge

This repository contains one deliberately small Home Assistant app: an OCPP
1.6J central system for a single EVBox Elvi, exposed to Home Assistant through
MQTT Discovery.

The app contains no charging, solar, scheduling, or load-balancing logic. Home
Assistant remains responsible for all automation decisions.

## Documentation

- [Standalone server and manual tests](docs/STANDALONE.md)
- [Automated build, mock, and tests](docs/VERIFY.md)
- [Home Assistant installation and migration](docs/HOME_ASSISTANT.md)
- [Home Assistant app-store documentation](evbox_elvi_ocpp/DOCS.md)
- [State/command contract and regression rationale](docs/ARCHITECTURE.md)

## Development checks

The tests use unittest and the pinned runtime protocol dependencies. Install
them in a local environment first (Python 3.13+):

```shell
python3 -m venv .venv
.venv/bin/python -m pip install -r evbox_elvi_ocpp/requirements.txt
PYTHONPATH=evbox_elvi_ocpp .venv/bin/python -m unittest discover -s tests -v
ruff check .
```

With Docker and Colima running, the complete image build, in-container tests,
temporary MQTT broker, and OCPP/MQTT smoke test are run with:

```shell
./dev_scripts/verify.sh
```

When behavior, entity IDs, configuration, images, or test coverage changes,
update the applicable guide in `docs/` in the same change. Keep
`evbox_elvi_ocpp/DOCS.md` aligned with the Home Assistant guide.
