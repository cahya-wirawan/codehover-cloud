# Model catalog

Compose mounts this folder read-only into the API and the worker at `/etc/codehover/catalog`
(`MODEL_CATALOG_HOST_DIR` in `.env` points it at another folder).

To offer paid presets:
1. Copy `../model-catalog.example.json` here as `model-catalog.json`.
2. Confirm every price, region and retention entry (the `VERIFY:` fields), and give the catalog its own `revision`.
3. In `.env`, set `MODEL_CATALOG_FILE=/etc/codehover/catalog/model-catalog.json` and `ANTHROPIC_API_KEY`.
4. Restart the API and the worker.

Catalog files here are not committed (`.gitignore`). A `MODEL_CATALOG_FILE` naming a missing or invalid
file stops the API and the worker at startup instead of silently offering only the replay preset.
