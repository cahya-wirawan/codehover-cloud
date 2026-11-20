# Model catalog

Compose mounts this folder read-only into the API and the worker at `/etc/codehover/catalog`
(`MODEL_CATALOG_HOST_DIR` in `.env` points it at another folder).

To offer paid presets:
1. Copy `../model-catalog.example.json` here as `model-catalog.json`.
2. Confirm every price, region and retention entry (the `VERIFY:` fields), and give the catalog its own `revision`.
3. In `.env`, set `MODEL_CATALOG_FILE=/etc/codehover/catalog/model-catalog.json` and the key of every
   provider your presets use: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `OPENROUTER_API_KEY`.
4. Restart the API and the worker.

A model's `provider` is `anthropic`, `openai`, `gemini` or `openrouter`, and `provider_model` is the name
that provider's API takes:
- OpenAI: the model ID for the Responses API.
- Gemini: the model code, without `models/`.
- OpenRouter: the model slug, such as `vendor/model`. The catalog `id` cannot contain a second `/`, so name it
  like `openrouter/vendor-model`.

Every model needs strict JSON-schema output (`structured_output: true`). Copy prices from the provider's
price page. For OpenRouter, the price is also the ceiling sent with every call: hosts that charge more are
skipped, and if none is cheaper the call fails. Only Anthropic models may have a `batch_price`. Gemini
presets accept `effort` up to `xhigh`; check which levels the chosen model supports.

Catalog files here are not committed (`.gitignore`). A `MODEL_CATALOG_FILE` naming a missing or invalid
file stops the API and the worker at startup instead of silently offering only the replay preset.
