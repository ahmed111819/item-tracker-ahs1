-- PostgreSQL storage shared by the three Amazon.sa deal scanners.
-- Run once using the database owner account. The scanner connects with a
-- separate limited role; do not expose the database without TLS/password auth.

BEGIN;

CREATE TABLE IF NOT EXISTS public.bot_state (
    bot_id text PRIMARY KEY,
    payload_gzip bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bot_state_bot_id_nonempty CHECK (length(trim(bot_id)) > 0)
);

CREATE TABLE IF NOT EXISTS public.alert_registry (
    alert_key text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('reserved', 'sent')),
    bot_id text NOT NULL,
    reserved_at timestamptz,
    sent_at timestamptz,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'reserved' AND sent_at IS NULL)
        OR (status = 'sent' AND sent_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS public.storage_migrations (
    migration_key text PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_registry_status_reserved_at_idx
    ON public.alert_registry (status, reserved_at);

CREATE OR REPLACE FUNCTION public.reserve_alert(
    p_alert_key text,
    p_bot_id text,
    p_token text,
    p_now timestamptz,
    p_ttl_seconds integer
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    was_reserved boolean;
BEGIN
    INSERT INTO public.alert_registry (
        alert_key, status, bot_id, reserved_at, sent_at, details, updated_at
    ) VALUES (
        p_alert_key, 'reserved', p_bot_id, p_now, NULL,
        jsonb_build_object('reservation_token', p_token), p_now
    )
    ON CONFLICT (alert_key) DO UPDATE
       SET status = 'reserved',
           bot_id = EXCLUDED.bot_id,
           reserved_at = EXCLUDED.reserved_at,
           sent_at = NULL,
           details = EXCLUDED.details,
           updated_at = EXCLUDED.updated_at
     WHERE public.alert_registry.status = 'reserved'
       AND (public.alert_registry.reserved_at IS NULL
            OR public.alert_registry.reserved_at <=
               p_now - make_interval(secs => p_ttl_seconds))
    RETURNING true INTO was_reserved;

    RETURN COALESCE(was_reserved, false);
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_alert(
    p_alert_key text,
    p_bot_id text,
    p_token text,
    p_success boolean,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    affected integer;
BEGIN
    IF p_success THEN
        UPDATE public.alert_registry
           SET status = 'sent', bot_id = p_bot_id, sent_at = p_now,
               updated_at = p_now,
               details = COALESCE(details, '{}'::jsonb) - 'reservation_token'
         WHERE alert_key = p_alert_key
           AND status = 'reserved'
           AND details ->> 'reservation_token' = p_token;
    ELSE
        DELETE FROM public.alert_registry
         WHERE alert_key = p_alert_key
           AND status = 'reserved'
           AND details ->> 'reservation_token' = p_token;
    END IF;

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected > 0;
END;
$$;

REVOKE ALL ON FUNCTION public.reserve_alert(text, text, text, timestamptz, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION public.finish_alert(text, text, text, boolean, timestamptz)
    FROM PUBLIC;

COMMIT;

