-- One-time migration for persistent temporary product blocks and callback polling.
-- Run as the PostgreSQL database owner in coupon_storage.

BEGIN;

CREATE TABLE IF NOT EXISTS public.product_blocks (
    asin text PRIMARY KEY CHECK (asin ~ '^[A-Z0-9]{10}$'),
    mode text NOT NULL CHECK (mode = 'temporary'),
    blocked_price double precision NOT NULL CHECK (blocked_price > 0),
    resume_price double precision NOT NULL CHECK (resume_price > 0),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'release_pending', 'released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    message_chat_id text,
    message_id bigint
);

CREATE INDEX IF NOT EXISTS product_blocks_status_idx
    ON public.product_blocks (status);

CREATE TABLE IF NOT EXISTS public.telegram_callback_state (
    state_key text PRIMARY KEY CHECK (state_key = 'main'),
    next_update_id bigint NOT NULL DEFAULT 0 CHECK (next_update_id >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.product_blocks TO coupon_bot;
GRANT SELECT, INSERT, UPDATE
    ON public.telegram_callback_state TO coupon_bot;

COMMIT;
