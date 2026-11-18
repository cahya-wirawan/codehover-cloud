-- 0019 rate limits shared by every API replica (06 "Initial rate policies"; replaces nothing per process).

-- Fixed-window counters keyed by an HMAC of policy and subject (a user ID, a client address or a
-- workspace ID): neither addresses nor IDs are stored in the clear. Windows older than two days are
-- removed by the retention sweep. No tenant data, so no row-level security.
CREATE TABLE rate_limits (
  bucket text NOT NULL CHECK (bucket ~ '^[0-9a-f]{32}$'),
  window_start timestamptz NOT NULL,
  count integer NOT NULL CHECK (count >= 0),
  PRIMARY KEY (bucket, window_start)
);
CREATE INDEX ix_rate_limits_window ON rate_limits(window_start);
GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limits TO app_runtime;
