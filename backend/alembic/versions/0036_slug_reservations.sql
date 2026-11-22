-- 0036 released site addresses stay reserved for their workspace (05 "Published sites", 09).
--
-- A published address is printed in links, bookmarks and MCP client settings. Before 0036 it was free
-- again the moment its publication let go of it (unpublish, a new address, a private site without one,
-- a project or workspace purge), so another workspace could publish its own site at the same address and
-- receive the old site's readers and agents. Now the address is reserved for the releasing workspace for
-- 90 days: that workspace may take it back at any time, every other workspace gets SLUG_TAKEN. A purged
-- workspace's addresses stay reserved for nobody until the period ends. Operator takedowns already keep
-- their address forever (0012, 0016: the row cannot be deleted).
--
-- The table is global (an address is unique across tenants), so the runtime cannot read or write it at
-- all: only the trigger functions below, owned by app_resolver, touch it. Rows hold the address, the
-- releasing workspace's ID and two times, nothing else.

CREATE TABLE slug_reservations (
  slug text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
  workspace_id uuid NOT NULL,  -- no foreign key: the reservation outlives a purged workspace
  released_at timestamptz NOT NULL DEFAULT now(),
  reserved_until timestamptz NOT NULL,
  CHECK (reserved_until > released_at)
);
CREATE INDEX ix_slug_reservations_until ON slug_reservations(reserved_until);
ALTER TABLE slug_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE slug_reservations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON slug_reservations FROM app_runtime;
CREATE POLICY resolver ON slug_reservations TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON slug_reservations TO app_resolver;

-- Every change that takes or lets go of an address first locks that address (and the other one, for a
-- change of address), in a fixed order. A release and a claim of the same address therefore never
-- interleave: a claim that waited sees the committed reservation, because each statement in a volatile
-- PL/pgSQL function reads a fresh snapshot under READ COMMITTED.
CREATE FUNCTION app_private.slug_claim() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  old_slug text := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN OLD.public_slug END;
  new_slug text := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN NEW.public_slug END;
BEGIN
  IF old_slug IS NOT DISTINCT FROM new_slug THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF old_slug IS NOT NULL AND (new_slug IS NULL OR old_slug < new_slug) THEN
    PERFORM pg_advisory_xact_lock(hashtextextended('publication-slug:' || old_slug, 0));
  END IF;
  IF new_slug IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(hashtextextended('publication-slug:' || new_slug, 0));
    IF old_slug IS NOT NULL AND old_slug > new_slug THEN
      PERFORM pg_advisory_xact_lock(hashtextextended('publication-slug:' || old_slug, 0));
    END IF;
    IF EXISTS (SELECT 1 FROM public.slug_reservations r
                WHERE r.slug = new_slug AND r.workspace_id <> NEW.workspace_id AND r.reserved_until > now()) THEN
      RAISE EXCEPTION 'this address was released recently and is reserved' USING ERRCODE = '23505',
        CONSTRAINT = 'publications_slug_reserved';
    END IF;
    -- Taken back by its own workspace, or the reservation has ended.
    DELETE FROM public.slug_reservations r WHERE r.slug = new_slug;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
ALTER FUNCTION app_private.slug_claim() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.slug_claim() FROM PUBLIC;
CREATE TRIGGER publications_slug_claim BEFORE INSERT OR UPDATE OF public_slug OR DELETE ON publications
  FOR EACH ROW EXECUTE FUNCTION app_private.slug_claim();

-- After the row has really let go of its address (another BEFORE trigger may still refuse the change).
CREATE FUNCTION app_private.slug_release() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.public_slug IS NOT NULL AND (TG_OP = 'DELETE' OR NEW.public_slug IS DISTINCT FROM OLD.public_slug) THEN
    INSERT INTO public.slug_reservations(slug, workspace_id, reserved_until)
    VALUES (OLD.public_slug, OLD.workspace_id, now() + interval '90 days')
    ON CONFLICT (slug) DO UPDATE
      SET workspace_id = EXCLUDED.workspace_id, released_at = now(), reserved_until = EXCLUDED.reserved_until;
  END IF;
  RETURN NULL;
END $$;
ALTER FUNCTION app_private.slug_release() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.slug_release() FROM PUBLIC;
CREATE TRIGGER publications_slug_release AFTER UPDATE OF public_slug OR DELETE ON publications
  FOR EACH ROW EXECUTE FUNCTION app_private.slug_release();

-- The hourly retention sweep removes reservations that have ended.
CREATE FUNCTION app_private.sweep_slug_reservations() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  n bigint;
BEGIN
  DELETE FROM public.slug_reservations WHERE reserved_until < now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
ALTER FUNCTION app_private.sweep_slug_reservations() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.sweep_slug_reservations() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.sweep_slug_reservations() TO app_runtime;
