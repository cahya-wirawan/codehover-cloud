-- 0012 operator suspensions survive unpublishing (T15 security review).

-- An operator takedown lives on the publication row; deleting the row would let an admin publish afresh
-- without the suspension. The service refuses that delete, and the database does too.
CREATE FUNCTION app_private.keep_operator_suspension() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.suspended_reason = 'operator' THEN
    RAISE EXCEPTION 'publication suspended by an operator' USING ERRCODE = '23514',
      CONSTRAINT = 'publications_keep_operator_suspension';
  END IF;
  RETURN OLD;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.keep_operator_suspension() FROM PUBLIC;
CREATE TRIGGER publications_keep_operator_suspension BEFORE DELETE ON publications
  FOR EACH ROW EXECUTE FUNCTION app_private.keep_operator_suspension();
