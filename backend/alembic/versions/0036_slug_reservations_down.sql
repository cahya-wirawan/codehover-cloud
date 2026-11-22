DROP TRIGGER publications_slug_release ON publications;
DROP TRIGGER publications_slug_claim ON publications;
DROP FUNCTION app_private.sweep_slug_reservations();
DROP FUNCTION app_private.slug_release();
DROP FUNCTION app_private.slug_claim();
DROP TABLE slug_reservations;
