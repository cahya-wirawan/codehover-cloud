-- 0021 symbol keys are unique per file, not per build (found by the M4 demo on a real package).

-- A symbol key is the qualified name inside its file (`Class.method`), so two files of one build can
-- both define `main` or `extract`. The index already addresses symbols as file + key, and generation
-- matches cards on (file, key); only this constraint assumed build-wide keys, and any repository with
-- a name repeated across files failed to index.
ALTER TABLE symbols DROP CONSTRAINT symbols_build_id_symbol_key_key;
ALTER TABLE symbols ADD CONSTRAINT symbols_build_file_key UNIQUE (build_id, source_file_id, symbol_key);
