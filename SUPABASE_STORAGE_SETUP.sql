-- Required before using Supabase Storage for compressed bot tracking state.
-- The bucket remains private; GitHub Actions accesses it with SUPABASE_SECRET_KEY.

begin;

insert into storage.buckets (
    id, name, public, file_size_limit, allowed_mime_types
) values (
    'scanner-state',
    'scanner-state',
    false,
    52428800,
    array['application/gzip']
)
on conflict (id) do update
set name = excluded.name,
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

commit;
