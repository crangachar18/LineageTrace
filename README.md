LineageTrace

A web app for collaborative entry and tracking of genetics lab products — experiments, runs, reagents, and inventory — built with FastAPI and a Supabase (Postgres + Auth + RLS) backend.

Overview

LineageTrace lets a lab team record experiments and their runs, manage shared reagent and antibody inventory, and trace the lineage of lab products through a browser interface. Authentication and authorization are handled through Supabase Auth (Google OAuth) with Postgres Row Level Security, so data access is gated at the database layer rather than the application layer alone.

The repository contains two backends behind a shared data-access module:


web_main_app/ — the browser-based FastAPI app (login, dashboard, experiment search/creation, inventory, workflows).
pyapp/ — the underlying data and business-logic layer (database access, antibody/secondary reagent rules, config, Supabase integration) shared by the web app.


Features


Google OAuth login via Supabase, with app-level email allowlisting plus database-enforced RLS
Dashboard and experiment search
Create and list experiments and experiment runs
Inventory management (list, edit, delete) for reagents and antibodies
Antibody / secondary-reagent rule handling
Guided IHC and PCR workflow pages
Protocol Builder draft page
Orphanage view for unlinked records
Access-request and supervisor-setup flows for onboarding team members
Backend switch between local SQLite and Supabase Postgres


Tech stack


Backend: Python, FastAPI, Uvicorn
Templates: Jinja2 (HTML), CSS
Database / Auth: Supabase (Postgres, Auth, Row Level Security, PL/pgSQL) or local SQLite
Schemas: JSON Schema definitions for experiment, run, lineage, and slide records (schemas/)
API spec: OpenAPI definition in docs/openapi.yaml


Repository structure

LineageTrace/
├── web_main_app/      Browser FastAPI app (app.py, templates/, static/)
├── pyapp/             Shared data layer, config, and reagent rules
├── schemas/           JSON Schema definitions + examples
├── scripts/           Supabase setup SQL (auth, RLS)
├── docs/              Supabase beta setup guide, OpenAPI spec
├── requirements.txt
└── lineagetrace_connections.env.example

Getting started

Prerequisites


Python 3.10+
A Supabase project (for the default supabase backend), or use local mode for a SQLite database


Installation

bashgit clone https://github.com/crangachar18/LineageTrace.git
cd LineageTrace

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

Configuration

Copy the example connection profile and fill in your values:

bashcp lineagetrace_connections.env.example lineagetrace_connections.env

Key settings (see the example file for full documentation):


SLIDEAPP_CONNECTION_MODE — supabase or local
LINEAGETRACE_WEB_SECRET — a long random string for session signing
SUPABASE_URL / SUPABASE_ANON_KEY — from your Supabase project's API settings (safe to expose publicly when RLS is enabled)
LINEAGETRACE_AUTHORIZED_EMAILS — optional app-level allowlist; the real write gate is public.authorized_users + RLS


The real lineagetrace_connections.env is git-ignored and should never be committed.

For Supabase setup (schema, auth, RLS policies), see docs/SUPABASE_BETA_SETUP.md and run the SQL in scripts/setup_supabase_direct_auth.sql.

Running

bashpython -m uvicorn web_main_app.app:app --reload --port 8010

Then open http://127.0.0.1:8010


For local Supabase OAuth testing, allow http://127.0.0.1:8010/auth/callback and http://localhost:8010/auth/callback as redirect URLs in Supabase and Google Cloud.



Data schemas

JSON Schema definitions for the core record types live in schemas/, with example payloads in schemas/examples/:


experiment-record.schema.json
experiment-run.min.schema.json
lineage-record.min.schema.json
slide-record.schema.json


Security notes


Database writes are gated by Supabase Row Level Security and public.authorized_users; the app-level email allowlist is a friendlier early rejection, not the primary control.
The Google OAuth client secret stays in Supabase / Google Cloud and is never committed.
Supabase anon keys are designed to be public only when RLS is correctly configured.


License

No license file is currently included. Add one to clarify reuse terms.
