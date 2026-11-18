-- 0009 build-scoped search documents (backlog T14; 05 "Search documents").

-- Derived rows for definitions, files and guide pages of one build. Search always filters workspace and
-- build before ranking; there is no global source index. The `simple` configuration keeps identifiers
-- intact; identifier parts (snake_case, camelCase) are added to `terms` so `parse_shape` also matches
-- `parse` and `shape`.
CREATE TABLE search_documents (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('symbol', 'file', 'guide')),
  ref_id uuid NOT NULL,
  name text NOT NULL CHECK (char_length(name) <= 1024),
  name_lower text GENERATED ALWAYS AS (lower(name)) STORED,
  path text,
  line integer CHECK (line IS NULL OR line >= 1),
  symbol_kind text,
  snippet text NOT NULL DEFAULT '' CHECK (char_length(snippet) <= 600),
  terms text NOT NULL DEFAULT '' CHECK (char_length(terms) <= 4000),
  body text NOT NULL DEFAULT '' CHECK (char_length(body) <= 20000),
  document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', name), 'A') || setweight(to_tsvector('simple', terms), 'B')
    || setweight(to_tsvector('simple', COALESCE(path, '')), 'C') || setweight(to_tsvector('simple', body), 'D')
  ) STORED,
  UNIQUE (build_id, kind, ref_id),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);
CREATE INDEX ix_search_documents_tsv ON search_documents USING gin (document);
CREATE INDEX ix_search_documents_name ON search_documents (build_id, name_lower text_pattern_ops);

GRANT SELECT, INSERT, DELETE ON search_documents TO app_runtime;
CREATE POLICY tenant ON search_documents TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE search_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_documents FORCE ROW LEVEL SECURITY;
