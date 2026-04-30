-- ============================================================
-- LineageTrace - Supabase schema for direct Auth + RLS beta
--
-- Run in the Supabase SQL Editor for the deployment project.
-- The beta client uses Supabase Auth + anon key. Never ship the
-- service-role key in the app.
-- ============================================================

create extension if not exists pgcrypto;
create schema if not exists private;

-- 1. Authorized users, signed-in users, and admin/researcher assignments
create table if not exists public.authorized_users (
    email text primary key,
    role text not null default 'researcher' check (role in ('main_admin', 'admin', 'researcher')),
    active boolean not null default true,
    display_name text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists idx_authorized_users_lower_email
    on public.authorized_users (lower(email));

create table if not exists public.access_requests (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    email text not null,
    display_name text not null default '',
    requested_role text not null check (requested_role in ('admin', 'researcher')),
    request_note text not null default '',
    status text not null default 'pending' check (status in ('pending', 'approved', 'denied')),
    requested_at timestamptz not null default now(),
    reviewed_at timestamptz,
    reviewed_by_user_id uuid references auth.users(id) on delete set null,
    review_note text not null default ''
);

create index if not exists idx_access_requests_status_requested_at
    on public.access_requests(status, requested_at desc);

create unique index if not exists idx_access_requests_one_pending_per_email
    on public.access_requests (lower(email))
    where status = 'pending';

create table if not exists public.app_users (
    user_id uuid primary key references auth.users(id) on delete cascade,
    email text not null unique,
    role text not null default 'researcher' check (role in ('main_admin', 'admin', 'researcher')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Repair projects that were initialized with an earlier beta schema.
alter table public.app_users
    add column if not exists created_at timestamptz not null default now(),
    add column if not exists updated_at timestamptz not null default now();

create unique index if not exists idx_app_users_email
    on public.app_users (email);

create table if not exists public.researcher_assignments (
    researcher_user_id uuid primary key references auth.users(id) on delete cascade,
    admin_user_id uuid not null references auth.users(id) on delete cascade,
    assigned_by_user_id uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now()
);

create index if not exists idx_researcher_assignments_admin_user_id
    on public.researcher_assignments(admin_user_id);

create table if not exists public.supervisor_requests (
    id uuid primary key default gen_random_uuid(),
    researcher_user_id uuid not null references auth.users(id) on delete cascade,
    researcher_email text not null,
    researcher_display_name text not null default '',
    admin_user_id uuid not null references auth.users(id) on delete cascade,
    admin_email text not null,
    status text not null default 'pending' check (status in ('pending', 'approved', 'denied', 'canceled')),
    requested_at timestamptz not null default now(),
    reviewed_at timestamptz,
    reviewed_by_user_id uuid references auth.users(id) on delete set null,
    review_note text not null default ''
);

create index if not exists idx_supervisor_requests_admin_status
    on public.supervisor_requests(admin_user_id, status, requested_at desc);

create index if not exists idx_supervisor_requests_researcher_status
    on public.supervisor_requests(researcher_user_id, status, requested_at desc);

create unique index if not exists idx_supervisor_requests_one_pending_per_researcher
    on public.supervisor_requests (researcher_user_id)
    where status = 'pending';

-- 2. Private helpers used by RLS. Keep these outside exposed schemas.
create or replace function private.current_app_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
    select au.role
    from public.authorized_users au
    where au.active
      and lower(au.email) = lower(auth.jwt()->>'email')
    limit 1
$$;

create or replace function private.is_authorized_app_user()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1
        from public.authorized_users au
        where au.active
          and lower(au.email) = lower(auth.jwt()->>'email')
    )
$$;

create or replace function private.is_inventory_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select coalesce(private.current_app_role() in ('main_admin', 'admin'), false)
$$;

create or replace function private.can_access_record_owner(owner_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select coalesce(
        private.is_authorized_app_user()
        and (
            owner_id = auth.uid()
            or private.current_app_role() = 'main_admin'
            or (
                private.current_app_role() = 'admin'
                and exists (
                    select 1
                    from public.researcher_assignments ra
                    where ra.admin_user_id = auth.uid()
                      and ra.researcher_user_id = owner_id
                )
            )
        ),
        false
    )
$$;

-- 3. Records
create table if not exists public.experiment_runs (
    run_id text primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    username text not null,
    created_at timestamptz not null,
    payload_json text not null
);

create index if not exists idx_experiment_runs_user_id
    on public.experiment_runs(user_id);

-- 4. Inventory
create table if not exists public.standards_objects (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    username text not null,
    category text not null check (
        category in (
            'slibrary',
            'mix_component',
            'mini_prep',
            'pcr_amplicon',
            'pcr_primer',
            'block',
            'stock',
            'plate',
            'rna_probe',
            'primary_antibody',
            'secondary_antibody',
            'restriction_enzyme',
            'antibody'
        )
    ),
    name text not null,
    visibility text not null default 'Personal' check (visibility in ('Shared', 'Personal')),
    metadata_json text not null default '{}',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, category, name)
);

alter table public.standards_objects
    add column if not exists visibility text not null default 'Personal'
    check (visibility in ('Shared', 'Personal'));

create index if not exists idx_standards_objects_user_id_category
    on public.standards_objects(user_id, category);

create index if not exists idx_standards_objects_visibility_category
    on public.standards_objects(visibility, category);

-- 5. Inventory deletion backups
create table if not exists public.inventory_backups (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    username text not null,
    category text not null,
    visibility_filter text not null default 'all',
    row_count integer not null default 0,
    snapshot_json jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_inventory_backups_category_created_at
    on public.inventory_backups(category, created_at desc);

-- 6. Storage suggestions
create table if not exists public.user_storage_locations (
    user_id uuid not null references auth.users(id) on delete cascade,
    username text not null,
    location text not null,
    last_used_at timestamptz not null default now(),
    primary key (user_id, location)
);

-- 7. Enable RLS
alter table public.authorized_users enable row level security;
alter table public.access_requests enable row level security;
alter table public.app_users enable row level security;
alter table public.researcher_assignments enable row level security;
alter table public.supervisor_requests enable row level security;
alter table public.experiment_runs enable row level security;
alter table public.standards_objects enable row level security;
alter table public.inventory_backups enable row level security;
alter table public.user_storage_locations enable row level security;

-- authorized_users policies
drop policy if exists "authorized_users_select_visible" on public.authorized_users;
create policy "authorized_users_select_visible"
on public.authorized_users
for select
to authenticated
using (
    private.current_app_role() = 'main_admin'
    or (
        private.is_authorized_app_user()
        and active
        and role in ('admin', 'main_admin')
    )
    or lower(email) = lower(auth.jwt()->>'email')
);

drop policy if exists "authorized_users_main_admin_write" on public.authorized_users;
create policy "authorized_users_main_admin_write"
on public.authorized_users
for all
to authenticated
using (private.current_app_role() = 'main_admin')
with check (private.current_app_role() = 'main_admin');

-- access_requests policies
drop policy if exists "access_requests_insert_own" on public.access_requests;
create policy "access_requests_insert_own"
on public.access_requests
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and user_id = (select auth.uid())
    and lower(email) = lower(auth.jwt()->>'email')
    and requested_role in ('admin', 'researcher')
    and status = 'pending'
);

drop policy if exists "access_requests_select_visible" on public.access_requests;
create policy "access_requests_select_visible"
on public.access_requests
for select
to authenticated
using (
    private.current_app_role() = 'main_admin'
    or (
        (select auth.uid()) is not null
        and user_id = (select auth.uid())
        and lower(email) = lower(auth.jwt()->>'email')
    )
);

drop policy if exists "access_requests_main_admin_update" on public.access_requests;
create policy "access_requests_main_admin_update"
on public.access_requests
for update
to authenticated
using (private.current_app_role() = 'main_admin')
with check (private.current_app_role() = 'main_admin');

-- app_users policies
drop policy if exists "app_users_select_visible" on public.app_users;
create policy "app_users_select_visible"
on public.app_users
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and (
        (select auth.uid()) = user_id
        or private.current_app_role() = 'main_admin'
        or role in ('admin', 'main_admin')
        or (
            private.current_app_role() = 'admin'
            and exists (
                select 1
                from public.researcher_assignments ra
                where ra.admin_user_id = (select auth.uid())
                  and ra.researcher_user_id = app_users.user_id
            )
        )
    )
);

drop policy if exists "app_users_insert_own_researcher" on public.app_users;
drop policy if exists "app_users_insert_own_authorized" on public.app_users;
create policy "app_users_insert_own_authorized"
on public.app_users
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and (select auth.uid()) = user_id
    and lower(auth.jwt()->>'email') = lower(email)
    and private.is_authorized_app_user()
    and role = private.current_app_role()
);

drop policy if exists "app_users_main_admin_update_roles" on public.app_users;
drop policy if exists "app_users_update_own_authorized" on public.app_users;
create policy "app_users_update_own_authorized"
on public.app_users
for update
to authenticated
using (
    (select auth.uid()) is not null
    and (select auth.uid()) = user_id
    and private.is_authorized_app_user()
)
with check (
    (select auth.uid()) is not null
    and (select auth.uid()) = user_id
    and lower(auth.jwt()->>'email') = lower(email)
    and private.is_authorized_app_user()
    and role = private.current_app_role()
);

create policy "app_users_main_admin_update_roles"
on public.app_users
for update
to authenticated
using (private.current_app_role() = 'main_admin')
with check (private.current_app_role() = 'main_admin');

-- researcher_assignments policies
drop policy if exists "researcher_assignments_select_visible" on public.researcher_assignments;
create policy "researcher_assignments_select_visible"
on public.researcher_assignments
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and (
        private.current_app_role() = 'main_admin'
        or admin_user_id = (select auth.uid())
        or researcher_user_id = (select auth.uid())
    )
);

drop policy if exists "researcher_assignments_main_admin_write" on public.researcher_assignments;
drop policy if exists "researcher_assignments_admin_approve_write" on public.researcher_assignments;
create policy "researcher_assignments_admin_approve_write"
on public.researcher_assignments
for all
to authenticated
using (
    private.current_app_role() = 'main_admin'
    or admin_user_id = (select auth.uid())
)
with check (
    private.current_app_role() = 'main_admin'
    or (
        private.current_app_role() = 'admin'
        and admin_user_id = (select auth.uid())
        and exists (
            select 1
            from public.supervisor_requests sr
            where sr.researcher_user_id = researcher_assignments.researcher_user_id
              and sr.admin_user_id = (select auth.uid())
              and sr.status = 'approved'
        )
    )
);

-- supervisor request policies
drop policy if exists "supervisor_requests_insert_own_researcher" on public.supervisor_requests;
create policy "supervisor_requests_insert_own_researcher"
on public.supervisor_requests
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and private.current_app_role() = 'researcher'
    and researcher_user_id = (select auth.uid())
    and lower(researcher_email) = lower(auth.jwt()->>'email')
    and status = 'pending'
    and exists (
        select 1
        from public.app_users au
        where au.user_id = supervisor_requests.admin_user_id
          and au.role = 'admin'
          and lower(au.email) = lower(supervisor_requests.admin_email)
    )
);

drop policy if exists "supervisor_requests_select_visible" on public.supervisor_requests;
create policy "supervisor_requests_select_visible"
on public.supervisor_requests
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and (
        private.current_app_role() = 'main_admin'
        or admin_user_id = (select auth.uid())
        or researcher_user_id = (select auth.uid())
    )
);

drop policy if exists "supervisor_requests_admin_review" on public.supervisor_requests;
create policy "supervisor_requests_admin_review"
on public.supervisor_requests
for update
to authenticated
using (
    (select auth.uid()) is not null
    and status = 'pending'
    and (
        private.current_app_role() = 'main_admin'
        or (
            private.current_app_role() = 'admin'
            and admin_user_id = (select auth.uid())
        )
    )
)
with check (
    (select auth.uid()) is not null
    and status in ('approved', 'denied', 'canceled')
    and reviewed_by_user_id = (select auth.uid())
    and reviewed_at is not null
    and (
        private.current_app_role() = 'main_admin'
        or (
            private.current_app_role() = 'admin'
            and admin_user_id = (select auth.uid())
        )
    )
);

-- experiment_runs policies
drop policy if exists "experiment_runs_select_visible" on public.experiment_runs;
create policy "experiment_runs_select_visible"
on public.experiment_runs
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and private.can_access_record_owner(user_id)
);

drop policy if exists "experiment_runs_insert_own" on public.experiment_runs;
create policy "experiment_runs_insert_own"
on public.experiment_runs
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and user_id = (select auth.uid())
    and private.is_authorized_app_user()
);

drop policy if exists "experiment_runs_update_visible" on public.experiment_runs;
create policy "experiment_runs_update_visible"
on public.experiment_runs
for update
to authenticated
using (
    (select auth.uid()) is not null
    and private.can_access_record_owner(user_id)
)
with check (
    (select auth.uid()) is not null
    and private.can_access_record_owner(user_id)
);

-- standards_objects policies
drop policy if exists "standards_objects_select_visible" on public.standards_objects;
create policy "standards_objects_select_visible"
on public.standards_objects
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and (
        visibility = 'Shared'
        or user_id = (select auth.uid())
    )
);

drop policy if exists "standards_objects_insert_by_role" on public.standards_objects;
create policy "standards_objects_insert_by_role"
on public.standards_objects
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and user_id = (select auth.uid())
    and private.is_authorized_app_user()
    and (
        (private.is_inventory_admin() and visibility in ('Shared', 'Personal'))
        or (private.current_app_role() = 'researcher' and visibility = 'Personal')
    )
);

drop policy if exists "standards_objects_update_admins_only" on public.standards_objects;
drop policy if exists "standards_objects_update_by_role" on public.standards_objects;
create policy "standards_objects_update_by_role"
on public.standards_objects
for update
to authenticated
using (
    private.is_authorized_app_user()
    and (
        (
            private.is_inventory_admin()
            and (
                visibility = 'Shared'
                or user_id = (select auth.uid())
            )
        )
        or (
            private.current_app_role() = 'researcher'
            and user_id = (select auth.uid())
            and visibility = 'Personal'
            and category in ('pcr_amplicon', 'slibrary')
        )
    )
)
with check (
    private.is_authorized_app_user()
    and (
        (
            private.is_inventory_admin()
            and (
                visibility = 'Shared'
                or user_id = (select auth.uid())
            )
        )
        or (
            private.current_app_role() = 'researcher'
            and user_id = (select auth.uid())
            and visibility = 'Personal'
            and category in ('pcr_amplicon', 'slibrary')
        )
    )
);

drop policy if exists "standards_objects_delete_admins_only" on public.standards_objects;
create policy "standards_objects_delete_admins_only"
on public.standards_objects
for delete
to authenticated
using (
    private.is_inventory_admin()
    and (
        visibility = 'Shared'
        or user_id = (select auth.uid())
    )
);

-- inventory_backups policies
drop policy if exists "inventory_backups_main_admin_select" on public.inventory_backups;
create policy "inventory_backups_main_admin_select"
on public.inventory_backups
for select
to authenticated
using (private.current_app_role() = 'main_admin');

drop policy if exists "inventory_backups_main_admin_insert" on public.inventory_backups;
create policy "inventory_backups_main_admin_insert"
on public.inventory_backups
for insert
to authenticated
with check (
    private.current_app_role() = 'main_admin'
    and user_id = (select auth.uid())
);

drop policy if exists "inventory_backups_main_admin_delete" on public.inventory_backups;
create policy "inventory_backups_main_admin_delete"
on public.inventory_backups
for delete
to authenticated
using (private.current_app_role() = 'main_admin');

-- user_storage_locations policies
drop policy if exists "user_storage_locations_select_own" on public.user_storage_locations;
create policy "user_storage_locations_select_own"
on public.user_storage_locations
for select
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and user_id = (select auth.uid())
);

drop policy if exists "user_storage_locations_insert_own" on public.user_storage_locations;
create policy "user_storage_locations_insert_own"
on public.user_storage_locations
for insert
to authenticated
with check (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and user_id = (select auth.uid())
);

drop policy if exists "user_storage_locations_update_own" on public.user_storage_locations;
create policy "user_storage_locations_update_own"
on public.user_storage_locations
for update
to authenticated
using (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and user_id = (select auth.uid())
)
with check (
    (select auth.uid()) is not null
    and private.is_authorized_app_user()
    and user_id = (select auth.uid())
);

-- User authorization setup before people sign in:
--
-- insert into public.authorized_users (email, role, display_name) values
--   ('you@example.com', 'main_admin', 'Your Name'),
--   ('admin@example.com', 'admin', 'Test Admin'),
--   ('researcher@example.com', 'researcher', 'Test Researcher')
-- on conflict (email) do update
-- set role = excluded.role,
--     display_name = excluded.display_name,
--     active = true,
--     updated_at = now();
--
-- Researcher/admin record sharing setup:
-- Researchers choose a signed-in admin from the Supervisor page.
-- The admin approves the request from the Research Team page.
-- The approved request writes public.researcher_assignments, and RLS then
-- allows that admin to view/update that researcher's records.
--
-- Manual fallback if needed:
--
-- insert into public.researcher_assignments (researcher_user_id, admin_user_id, assigned_by_user_id)
-- select r.user_id, a.user_id, m.user_id
-- from public.app_users r
-- cross join public.app_users a
-- cross join public.app_users m
-- where r.email = 'researcher@example.com'
--   and a.email = 'admin@example.com'
--   and m.email = 'you@example.com'
-- on conflict (researcher_user_id) do update
-- set admin_user_id = excluded.admin_user_id,
--     assigned_by_user_id = excluded.assigned_by_user_id;
