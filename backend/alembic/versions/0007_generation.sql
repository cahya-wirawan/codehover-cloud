-- 0007 generation cache, file explanations and guide pages (backlog T11; 05, 07).

-- ---------------------------------------------------------------- tenant-local generation cache (07)
-- A validated model output keyed by every input that can change it (workspace, source and context
-- digests, engine and prompt-set MAJOR.MINOR, schema, preset, variant, selection). It is written in the
-- same fenced transaction that settles the call, so it doubles as the stage checkpoint: a retried task or
-- a retry build reuses it instead of paying again. Reuse never crosses the workspace boundary.
CREATE TABLE generation_cache (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  cache_key text NOT NULL CHECK (cache_key ~ '^[0-9a-f]{64}$'),
  purpose text NOT NULL CHECK (purpose ~ '^[a-z][a-z_]{0,31}$'),
  output jsonb NOT NULL CHECK (jsonb_typeof(output) = 'object' AND pg_column_size(output) <= 262144),
  output_digest text NOT NULL CHECK (output_digest ~ '^[0-9a-f]{64}$'),
  model_id text NOT NULL,
  prompt_version text NOT NULL,
  attempt_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days',
  UNIQUE (workspace_id, cache_key),
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES provider_attempts(workspace_id, id)
);
CREATE INDEX ix_generation_cache_expiry ON generation_cache(expires_at);

-- ---------------------------------------------------------------- per-file explanations (build-scoped)
CREATE TABLE file_explanations (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  source_file_id uuid NOT NULL,
  -- generated: model output for every chunk; partial: some chunks fell back to source-only cards;
  -- failed: no valid model output (source-only cards).
  status text NOT NULL CHECK (status IN ('generated', 'partial', 'failed')),
  summary text NOT NULL DEFAULT '' CHECK (char_length(summary) <= 20000),
  chunks jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(chunks) = 'array'),
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
  UNIQUE (build_id, source_file_id),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, source_file_id) REFERENCES source_files(workspace_id, id)
);

-- ---------------------------------------------------------------- guide pages (05)
CREATE TABLE guide_pages (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  slug text NOT NULL CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'),
  title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 8),
  purpose text NOT NULL DEFAULT '' CHECK (char_length(purpose) <= 1000),
  source_ranges jsonb NOT NULL CHECK (jsonb_typeof(source_ranges) = 'array'),
  status text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'written', 'failed')),
  markdown_key text CHECK (markdown_key IS NULL OR markdown_key ~ '^workspaces/[0-9a-f-]{36}/[A-Za-z0-9._/-]+$'),
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
  check_status text NOT NULL DEFAULT 'unchecked'
    CHECK (check_status IN ('unchecked', 'supported', 'partial', 'unsupported', 'invalid')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((status = 'written') = (markdown_key IS NOT NULL)),
  UNIQUE (build_id, slug),
  UNIQUE (build_id, ordinal),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);

-- ---------------------------------------------------------------- privileges and RLS
GRANT SELECT, INSERT, UPDATE, DELETE ON generation_cache, file_explanations, guide_pages TO app_runtime;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['generation_cache', 'file_explanations', 'guide_pages'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
