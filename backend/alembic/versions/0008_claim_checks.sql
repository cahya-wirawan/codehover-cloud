-- 0008 claim checks and waivers (backlog T12; 05, 07).

-- One row per extracted claim and cited range (05). The deterministic result is final when invalid;
-- otherwise the checker's classification applies to every row of the claim. A repaired page keeps its
-- original rows (current = false) next to the rows of the repaired text, so evidence is never deleted.
CREATE TABLE claim_checks (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  guide_page_id uuid NOT NULL,
  claim_key text NOT NULL CHECK (claim_key ~ '^[0-9a-f]{32}$'),
  claim_text text NOT NULL CHECK (char_length(claim_text) <= 4000),
  ordinal integer NOT NULL CHECK (ordinal >= 1),
  revision smallint NOT NULL DEFAULT 0 CHECK (revision IN (0, 1)),
  current boolean NOT NULL DEFAULT true,
  citation text NOT NULL CHECK (char_length(citation) <= 200),
  source_file_id uuid,
  path text,
  start_line integer NOT NULL CHECK (start_line >= 0),
  end_line integer NOT NULL CHECK (end_line >= start_line),
  source_hash text CHECK (source_hash IS NULL OR source_hash ~ '^[0-9a-f]{64}$'),
  deterministic text NOT NULL CHECK (deterministic IN ('valid', 'invalid')),
  status text NOT NULL CHECK (status IN ('supported', 'partial', 'unsupported', 'unchecked', 'invalid')),
  reason text NOT NULL DEFAULT '' CHECK (char_length(reason) <= 1000),
  evidence_lines jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_lines) = 'array'),
  checker_model text,
  waived_by uuid REFERENCES users(id),
  waived_reason text CHECK (waived_reason IS NULL OR char_length(waived_reason) BETWEEN 10 AND 1000),
  waived_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  checked_at timestamptz,
  -- Invalid citations are final and can never be waived; only checked unsupported/partial claims can.
  CHECK ((deterministic = 'invalid') = (status = 'invalid')),
  CHECK (waived_at IS NULL OR status IN ('unsupported', 'partial')),
  CHECK ((waived_at IS NULL) = (waived_by IS NULL) AND (waived_at IS NULL) = (waived_reason IS NULL)),
  UNIQUE (guide_page_id, claim_key, citation),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, guide_page_id) REFERENCES guide_pages(workspace_id, id),
  FOREIGN KEY (workspace_id, source_file_id) REFERENCES source_files(workspace_id, id)
);
CREATE INDEX ix_claim_checks_build_status ON claim_checks(build_id, status) WHERE current;

-- The page text before its repair cycle (19 P06: preserve original and repaired versions).
ALTER TABLE guide_pages ADD COLUMN original_markdown_key text
  CHECK (original_markdown_key IS NULL OR original_markdown_key ~ '^workspaces/[0-9a-f-]{36}/[A-Za-z0-9._/-]+$');

GRANT SELECT, INSERT, UPDATE ON claim_checks TO app_runtime;
CREATE POLICY tenant ON claim_checks TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE claim_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE claim_checks FORCE ROW LEVEL SECURITY;
