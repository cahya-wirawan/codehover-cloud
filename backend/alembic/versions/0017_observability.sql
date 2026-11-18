-- 0017 operational gauges (backlog T18; 12 "Dashboards" and "Initial alert thresholds").

-- One aggregate row for the scheduler's metrics: counts, ages in seconds and a money drift, never IDs,
-- names or content. The resolver role sees only the columns the aggregates need, through row policies
-- that exist for this function alone.
CREATE POLICY resolver_gauges ON build_tasks FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_gauges ON outbox FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_gauges ON provider_attempts FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_gauges ON usage_accounts FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_gauges ON usage_entries FOR SELECT TO app_resolver USING (true);
GRANT SELECT (state, next_attempt_at, lease_until, created_at) ON build_tasks TO app_resolver;
GRANT SELECT (published_at, available_at) ON outbox TO app_resolver;
GRANT SELECT (state, created_at) ON provider_attempts TO app_resolver;
GRANT SELECT (id, reserved_micro_usd, settled_micro_usd) ON usage_accounts TO app_resolver;
GRANT SELECT (account_id, kind, amount_micro_usd) ON usage_entries TO app_resolver;
GRANT SELECT (requested_at) ON deletion_requests TO app_resolver;

CREATE FUNCTION app_private.ops_gauges()
  RETURNS TABLE (
    runnable_tasks bigint, oldest_runnable_seconds double precision, leased_tasks bigint,
    expired_leases bigint, outbox_unpublished bigint, outbox_oldest_seconds double precision,
    deletions_pending bigint, deletion_oldest_seconds double precision, unknown_attempts bigint,
    unknown_oldest_seconds double precision, usage_drift_micro_usd numeric)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT
    (SELECT count(*) FROM public.build_tasks t WHERE t.state IN ('pending', 'retry_wait')
       AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())),
    (SELECT COALESCE(max(extract(epoch FROM now() - COALESCE(t.next_attempt_at, t.created_at))), 0)
       FROM public.build_tasks t WHERE t.state IN ('pending', 'retry_wait')
       AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())),
    (SELECT count(*) FROM public.build_tasks t WHERE t.state = 'leased' AND t.lease_until >= now()),
    (SELECT count(*) FROM public.build_tasks t WHERE t.state = 'leased' AND t.lease_until < now()),
    (SELECT count(*) FROM public.outbox o WHERE o.published_at IS NULL),
    (SELECT COALESCE(max(extract(epoch FROM now() - o.available_at)), 0) FROM public.outbox o
       WHERE o.published_at IS NULL AND o.available_at <= now()),
    (SELECT count(*) FROM public.deletion_requests d WHERE d.state <> 'completed'),
    (SELECT COALESCE(max(extract(epoch FROM now() - d.requested_at)), 0) FROM public.deletion_requests d
       WHERE d.state <> 'completed'),
    (SELECT count(*) FROM public.provider_attempts a WHERE a.state = 'unknown'),
    (SELECT COALESCE(max(extract(epoch FROM now() - a.created_at)), 0) FROM public.provider_attempts a
       WHERE a.state = 'unknown'),
    -- Counters against the append-only ledger: reserved = reserve - settle - release, settled = settle.
    (SELECT COALESCE(sum(abs(u.reserved_micro_usd - COALESCE(l.reserved, 0)) + abs(u.settled_micro_usd - COALESCE(l.settled, 0))), 0)
       FROM public.usage_accounts u
       LEFT JOIN (SELECT e.account_id,
                         sum(CASE e.kind WHEN 'reserve' THEN e.amount_micro_usd
                                         WHEN 'settle' THEN -e.amount_micro_usd
                                         WHEN 'release' THEN -e.amount_micro_usd ELSE 0 END) AS reserved,
                         sum(CASE e.kind WHEN 'settle' THEN e.amount_micro_usd ELSE 0 END) AS settled
                    FROM public.usage_entries e GROUP BY e.account_id) l ON l.account_id = u.id)
$$;
ALTER FUNCTION app_private.ops_gauges() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.ops_gauges() FROM PUBLIC;
GRANT USAGE ON SCHEMA app_private TO app_system;
GRANT EXECUTE ON FUNCTION app_private.ops_gauges() TO app_system;
