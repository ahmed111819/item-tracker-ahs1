-- Required before enabling the all-bots Supabase workflow.
-- The existing bot_state and alert_registry tables remain in use.

begin;

create table if not exists public.registry_migrations (
    migration_key text primary key,
    completed_at timestamptz not null default now()
);

alter table public.registry_migrations enable row level security;
revoke all on public.registry_migrations from anon, authenticated;
grant all on public.registry_migrations to service_role;

create or replace function public.reserve_alert(
    p_alert_key text,
    p_bot_id text,
    p_token text,
    p_now timestamptz,
    p_ttl_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    was_reserved boolean;
begin
    insert into public.alert_registry (
        alert_key, status, bot_id, reserved_at, sent_at, details, updated_at
    ) values (
        p_alert_key, 'reserved', p_bot_id, p_now, null,
        jsonb_build_object('reservation_token', p_token), p_now
    )
    on conflict (alert_key) do update
    set status = 'reserved',
        bot_id = excluded.bot_id,
        reserved_at = excluded.reserved_at,
        sent_at = null,
        details = excluded.details,
        updated_at = excluded.updated_at
    where public.alert_registry.status = 'reserved'
      and (
        public.alert_registry.reserved_at is null
        or public.alert_registry.reserved_at <=
           p_now - make_interval(secs => p_ttl_seconds)
      )
    returning true into was_reserved;

    return coalesce(was_reserved, false);
end;
$$;

create or replace function public.finish_alert(
    p_alert_key text,
    p_bot_id text,
    p_token text,
    p_success boolean,
    p_now timestamptz
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    affected integer;
begin
    if p_success then
        update public.alert_registry
        set status = 'sent',
            bot_id = p_bot_id,
            sent_at = p_now,
            updated_at = p_now,
            details = coalesce(details, '{}'::jsonb) - 'reservation_token'
        where alert_key = p_alert_key
          and status = 'reserved'
          and details ->> 'reservation_token' = p_token;
    else
        delete from public.alert_registry
        where alert_key = p_alert_key
          and status = 'reserved'
          and details ->> 'reservation_token' = p_token;
    end if;

    get diagnostics affected = row_count;
    return affected > 0;
end;
$$;

revoke all on function public.reserve_alert(text, text, text, timestamptz, integer)
    from public, anon, authenticated;
revoke all on function public.finish_alert(text, text, text, boolean, timestamptz)
    from public, anon, authenticated;
grant execute on function public.reserve_alert(text, text, text, timestamptz, integer)
    to service_role;
grant execute on function public.finish_alert(text, text, text, boolean, timestamptz)
    to service_role;

commit;
