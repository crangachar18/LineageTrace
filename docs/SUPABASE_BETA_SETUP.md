# Supabase Beta Setup

This beta should use Supabase Auth with the public anon key plus Row Level Security. Do not ship the service-role key in the app.

## Project Setup

1. In Supabase, open the project for this deployment.
2. In Authentication, enable Google sign-in and/or email/password sign-in for the beta accounts.
3. Run `scripts/setup_supabase_direct_auth.sql` in the Supabase SQL Editor.
4. In the SQL Editor, authorize the exact emails that may use this deployment:

```sql
insert into public.authorized_users (email, role, display_name) values
  ('you@example.com', 'main_admin', 'Your Name'),
  ('admin@example.com', 'admin', 'Test Admin'),
  ('researcher@example.com', 'researcher', 'Test Researcher')
on conflict (email) do update
set role = excluded.role,
    display_name = excluded.display_name,
    active = true,
    updated_at = now();
```

`display_name` is the human-readable name shown in the app and written into inventory/record metadata. Change it here if someone changes names or if you want initials instead of full names.

5. Sign in once as each account from the app so `public.app_users` gets a row mapped to the Supabase Auth user id.
6. Researchers can request an admin supervisor from the in-app Supervisor page. The selected admin approves the relationship from the Research Team page.

## Local Web App Connection

Run the web app with the project URL and anon key. The preferred local setup is a swappable connection profile:

```bash
cp lineagetrace_connections.env.example lineagetrace_connections.env
```

Then edit `lineagetrace_connections.env` with the Supabase URL, Supabase anon key, and a stable `LINEAGETRACE_WEB_SECRET`. This file is ignored by git, so it can hold local project settings without being committed. To point the same code at a different Supabase or Google OAuth project later, change this file and restart the web app.

The app loads configuration in this order:

1. Shell environment variables
2. Repository `.env`
3. Repository `lineagetrace_connections.env`
4. Optional file named by `LINEAGETRACE_CONFIG_FILE`

Existing shell variables win over file values. This makes deployment flexible: local testing can use `lineagetrace_connections.env`, while a hosted website can use platform-managed environment variables.

You can also export the same values in the shell:

```bash
export SLIDEAPP_CONNECTION_MODE=supabase
export SUPABASE_URL=https://xxxxx.supabase.co
export SUPABASE_ANON_KEY=eyJ...
venv/bin/python -m uvicorn web_main_app.app:app --host 127.0.0.1 --port 8010
```

Or put them in a local `.env` file at the repository root:

```bash
SLIDEAPP_CONNECTION_MODE=supabase
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
LINEAGETRACE_WEB_SECRET=replace-with-a-long-random-string
LINEAGETRACE_AUTHORIZED_EMAILS=you@example.com,admin@example.com,researcher@example.com
```

When Supabase is configured, the login screen shows a Google sign-in button. Google OAuth client ID and secret normally stay configured inside Supabase, not in the app. Google should redirect back to:

```text
http://127.0.0.1:8010/auth/callback
```

## Role Rules

- `main_admin`: elevated beta owner; can update user roles, manage assignments, see all records, and manage shared inventory.
- `admin`: can see and modify records for assigned assistant researchers; can add, edit, and delete shared inventory rows.
- `researcher`: can add inventory rows only with `Personal` visibility; cannot update or delete inventory rows.

Records are written to `public.experiment_runs` under the signed-in user. Inventory is written to `public.standards_objects`. Personal inventory rows are visible only to their owner, even to `main_admin`.

## Authorized Login Emails

The database-level source of truth is `public.authorized_users`. RLS policies use that table to decide whether a signed-in user can read or write app data. If an email is not active in `authorized_users`, Supabase blocks records, inventory, storage suggestions, and backup writes even if Google/Supabase Auth accepts the login.

Unapproved Google users are shown an access request form. Their request is written to `public.access_requests` using their Google/Supabase session, but they still cannot read or write app data until a `main_admin` approves them into `public.authorized_users`.

`LINEAGETRACE_AUTHORIZED_EMAILS` in the connection profile is only a legacy friendly app-level hint. Do not rely on it as the security boundary.

## Access Request Review

Main admins can review requests inside the app at `/access-requests`. Approval writes the requester into `public.authorized_users` with the selected role and display name. On their next Google sign-in, the user gets the approved role automatically.

## Inventory Delete Backups

The Inventory page includes a `main_admin`-only Delete All action for each sheet. Before rows are deleted, the app writes the full visible row snapshot to `public.inventory_backups`. The delete is blocked if the backup write fails.

If this feature was added after the initial database setup, re-run `scripts/setup_supabase_direct_auth.sql` in the Supabase SQL Editor. The script is idempotent and will add `inventory_backups` without removing existing data.
