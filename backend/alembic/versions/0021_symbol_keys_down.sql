-- Builds with a name repeated across files cannot satisfy the old build-wide key: suffix the repeats.
UPDATE symbols s SET symbol_key = left(s.symbol_key, 1000) || '#' || substr(md5(s.id::text), 1, 8)
 WHERE EXISTS (SELECT 1 FROM symbols o WHERE o.build_id = s.build_id AND o.symbol_key = s.symbol_key AND o.id < s.id);
ALTER TABLE symbols DROP CONSTRAINT symbols_build_file_key;
ALTER TABLE symbols ADD CONSTRAINT symbols_build_id_symbol_key_key UNIQUE (build_id, symbol_key);
