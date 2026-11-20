-- 0030: a workspace's own key may be for OpenAI, Gemini or OpenRouter as well as Anthropic (07 BYOK).
-- Batches stay Anthropic-only: no other gateway submits them, and the catalog refuses other batch prices.
ALTER TABLE provider_keys DROP CONSTRAINT provider_keys_provider_check;
ALTER TABLE provider_keys ADD CONSTRAINT provider_keys_provider_check
  CHECK (provider IN ('anthropic', 'openai', 'gemini', 'openrouter'));
