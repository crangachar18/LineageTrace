from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pyapp.app_config import (
    SessionSettings,
    clear_session_settings,
    load_connection_settings,
    load_session_settings,
    save_session_settings,
)


class SupabaseSchemaNotReadyError(RuntimeError):
    pass


class SupabaseAuthExpiredError(RuntimeError):
    pass


def init_auth_db() -> None:
    # Direct Supabase mode relies on tables already created in the project.
    return None


def verify_credentials(username: str, password: str) -> str | None:
    connection = load_connection_settings()
    if connection is None or connection.mode != "supabase":
        raise RuntimeError("Supabase connection settings are not configured.")
    email = username.strip()
    payload = {
        "email": email,
        "password": password,
    }
    url = f"{connection.server_url.rstrip('/')}/auth/v1/token?grant_type=password"
    headers = {
        "apikey": connection.anon_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "LineageTraceBeta/0.1",
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8")
    except HTTPError:
        return None
    except URLError as exc:
        raise RuntimeError(f"Could not reach Supabase at {connection.server_url}.") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    access_token = str(data.get("access_token", "")).strip()
    refresh_token = str(data.get("refresh_token", "")).strip()
    if not access_token:
        return None

    return establish_session(access_token, refresh_token, fallback_username=email)


def establish_session(access_token: str, refresh_token: str = "", fallback_username: str = "") -> str | None:
    connection = load_connection_settings()
    if connection is None or connection.mode != "supabase":
        raise RuntimeError("Supabase connection settings are not configured.")
    clean_access_token = access_token.strip()
    if not clean_access_token:
        return None
    claims = _decode_jwt_payload(clean_access_token)
    user_id = str(claims.get("sub", "")).strip()
    email = fallback_username.strip() or str(claims.get("email", "")).strip()
    if not user_id or not email:
        return None
    session = SessionSettings(
        connection_mode="supabase",
        server_url=connection.server_url,
        username=email,
        display_name="",
        role="researcher",
        access_token=clean_access_token,
        refresh_token=refresh_token.strip(),
    )
    save_session_settings(session)
    ctx = _Context(connection.server_url, connection.anon_key, clean_access_token, user_id, email)
    profile = _lookup_authorized_user(ctx)
    if profile is None:
        raise RuntimeError("This Google account is not approved for LineageTrace yet.")
    _ensure_current_user_row(ctx, role=profile["role"])
    session.role = profile["role"]
    session.display_name = profile["display_name"]
    save_session_settings(session)
    return session.role


def submit_access_request(
    access_token: str,
    *,
    requested_role: str,
    display_name: str,
    request_note: str = "",
) -> None:
    ctx = _context_from_access_token(access_token)
    role = requested_role.strip().lower()
    if role not in {"admin", "researcher"}:
        raise ValueError("Requested role must be admin or researcher.")
    clean_display_name = display_name.strip()
    if not clean_display_name:
        clean_display_name = _display_name_from_email(ctx.username)
    now = _now_iso()
    try:
        _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/access_requests",
            payload={
                "user_id": ctx.user_id,
                "email": ctx.username,
                "display_name": clean_display_name,
                "requested_role": role,
                "request_note": request_note.strip(),
                "status": "pending",
                "requested_at": now,
            },
            extra_headers={"Prefer": "return=minimal"},
        )
    except RuntimeError as exc:
        message = str(exc)
        if "duplicate key" in message or "idx_access_requests_one_pending_per_email" in message:
            raise ValueError("An access request for this email is already pending.") from exc
        raise


def list_access_requests(status: str = "pending") -> list[dict[str, str]]:
    ctx = _require_context()
    query = {
        "select": "id,email,display_name,requested_role,request_note,status,requested_at,reviewed_at,review_note",
        "order": "requested_at.desc",
    }
    clean_status = status.strip().lower()
    if clean_status in {"pending", "approved", "denied"}:
        query["status"] = f"eq.{clean_status}"
    rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/access_requests",
        query=query,
    )
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append({key: str(row.get(key, "")).strip() for key in row})
    return out


def list_available_admins() -> list[dict[str, str]]:
    ctx = _require_context()
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/app_users",
            query={
                "select": "user_id,email,role",
                "role": "eq.admin",
                "order": "email.asc",
            },
        )
    except SupabaseSchemaNotReadyError:
        return []
    admins: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        admins.append(
            {
                "user_id": str(row.get("user_id", "")).strip(),
                "email": str(row.get("email", "")).strip(),
                "role": str(row.get("role", "")).strip(),
                "display_name": _display_name_from_email(str(row.get("email", "")).strip()),
            }
        )
    return [item for item in admins if item["user_id"] and item["email"]]


def list_visible_record_owners(role: str = "researcher") -> list[dict[str, str]]:
    ctx = _require_context()
    query = {
        "select": "user_id,email,role",
        "order": "email.asc",
    }
    clean_role = role.strip().lower()
    if clean_role in {"main_admin", "admin", "researcher"}:
        query["role"] = f"eq.{clean_role}"
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/app_users",
            query=query,
        )
    except SupabaseSchemaNotReadyError:
        return []
    owners: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        email = str(row.get("email", "")).strip()
        if not email:
            continue
        owners.append(
            {
                "user_id": str(row.get("user_id", "")).strip(),
                "email": email,
                "role": str(row.get("role", "")).strip(),
                "display_name": _display_name_from_email(email),
            }
        )
    return owners


def get_supervisor_status() -> dict[str, str]:
    ctx = _require_context()
    try:
        assignment_rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/researcher_assignments",
            query={
                "select": "researcher_user_id,admin_user_id,created_at",
                "researcher_user_id": f"eq.{ctx.user_id}",
                "limit": "1",
            },
        )
    except SupabaseSchemaNotReadyError:
        return {"state": "not_configured"}
    if isinstance(assignment_rows, list) and assignment_rows:
        admin_user_id = str(assignment_rows[0].get("admin_user_id", "")).strip()
        admin_email = _email_for_user_id(ctx, admin_user_id)
        return {
            "state": "assigned",
            "admin_user_id": admin_user_id,
            "admin_email": admin_email,
            "admin_display_name": _display_name_from_email(admin_email),
            "created_at": str(assignment_rows[0].get("created_at", "")).strip(),
        }

    request_rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/supervisor_requests",
        query={
            "select": "id,admin_user_id,admin_email,status,requested_at,reviewed_at,review_note",
            "researcher_user_id": f"eq.{ctx.user_id}",
            "order": "requested_at.desc",
            "limit": "1",
        },
    )
    if isinstance(request_rows, list) and request_rows:
        row = request_rows[0]
        if isinstance(row, dict):
            return {
                "state": str(row.get("status", "")).strip() or "pending",
                "request_id": str(row.get("id", "")).strip(),
                "admin_user_id": str(row.get("admin_user_id", "")).strip(),
                "admin_email": str(row.get("admin_email", "")).strip(),
                "admin_display_name": _display_name_from_email(str(row.get("admin_email", "")).strip()),
                "requested_at": str(row.get("requested_at", "")).strip(),
                "reviewed_at": str(row.get("reviewed_at", "")).strip(),
                "review_note": str(row.get("review_note", "")).strip(),
            }
    return {"state": "unassigned"}


def submit_supervisor_request(admin_user_id: str) -> None:
    ctx = _require_context()
    admin_id = admin_user_id.strip()
    if not admin_id:
        raise ValueError("Choose an admin supervisor.")
    session = load_session_settings()
    if session is None or session.role != "researcher":
        raise ValueError("Only researchers need to request an admin supervisor.")
    status = get_supervisor_status()
    if status.get("state") == "assigned":
        raise ValueError("This researcher already has an admin supervisor.")
    if status.get("state") == "pending":
        raise ValueError("A supervisor request is already pending.")
    admin_rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/app_users",
        query={
            "select": "user_id,email,role",
            "user_id": f"eq.{admin_id}",
            "role": "eq.admin",
            "limit": "1",
        },
    )
    if not isinstance(admin_rows, list) or not admin_rows or not isinstance(admin_rows[0], dict):
        raise ValueError("That admin is not available for assignment yet.")
    admin_email = str(admin_rows[0].get("email", "")).strip()
    if not admin_email:
        raise ValueError("That admin is missing an email address.")
    _rest_json(
        ctx,
        method="POST",
        path="/rest/v1/supervisor_requests",
        payload={
            "researcher_user_id": ctx.user_id,
            "researcher_email": ctx.username,
            "researcher_display_name": session.display_name or _display_name_from_email(ctx.username),
            "admin_user_id": admin_id,
            "admin_email": admin_email,
            "status": "pending",
            "requested_at": _now_iso(),
        },
        extra_headers={"Prefer": "return=minimal"},
    )


def list_supervisor_requests(status: str = "pending") -> list[dict[str, str]]:
    ctx = _require_context()
    query = {
        "select": "id,researcher_user_id,researcher_email,researcher_display_name,admin_user_id,admin_email,status,requested_at,reviewed_at,review_note",
        "order": "requested_at.desc",
    }
    clean_status = status.strip().lower()
    if clean_status in {"pending", "approved", "denied", "canceled"}:
        query["status"] = f"eq.{clean_status}"
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/supervisor_requests",
            query=query,
        )
    except SupabaseSchemaNotReadyError:
        return []
    requests: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out = {key: str(row.get(key, "")).strip() for key in row}
        out["researcher_label"] = out.get("researcher_display_name") or _display_name_from_email(out.get("researcher_email", ""))
        out["admin_label"] = _display_name_from_email(out.get("admin_email", ""))
        requests.append(out)
    return requests


def review_supervisor_request(request_id: str, action: str, review_note: str = "") -> None:
    ctx = _require_context()
    clean_id = request_id.strip()
    clean_action = action.strip().lower()
    if clean_action not in {"approve", "deny"} or not clean_id:
        raise ValueError("Choose approve or deny.")
    rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/supervisor_requests",
        query={
            "select": "id,researcher_user_id,researcher_email,admin_user_id,admin_email,status",
            "id": f"eq.{clean_id}",
            "status": "eq.pending",
            "limit": "1",
        },
    )
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError("Supervisor request was not found.")
    row = rows[0]
    new_status = "approved" if clean_action == "approve" else "denied"
    now = _now_iso()
    _rest_json(
        ctx,
        method="PATCH",
        path="/rest/v1/supervisor_requests",
        query={"id": f"eq.{clean_id}"},
        payload={
            "status": new_status,
            "reviewed_at": now,
            "reviewed_by_user_id": ctx.user_id,
            "review_note": review_note.strip(),
        },
        extra_headers={"Prefer": "return=minimal"},
    )
    if new_status != "approved":
        return
    _rest_json(
        ctx,
        method="POST",
        path="/rest/v1/researcher_assignments",
        query={"on_conflict": "researcher_user_id"},
        payload={
            "researcher_user_id": str(row.get("researcher_user_id", "")).strip(),
            "admin_user_id": str(row.get("admin_user_id", "")).strip(),
            "assigned_by_user_id": ctx.user_id,
            "created_at": now,
        },
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def approve_access_request(request_id: str, role: str, display_name: str = "") -> None:
    _review_access_request(request_id, action="approved", role=role, display_name=display_name)


def deny_access_request(request_id: str, review_note: str = "") -> None:
    _review_access_request(request_id, action="denied", role="", display_name="", review_note=review_note)


def save_experiment_payload(username: str, payload: dict[str, Any]) -> tuple[str, Path]:
    ctx = _require_context()
    _ensure_current_user_row(ctx)

    run_id = uuid.uuid4().hex
    created_at = _now_iso()
    payload_json = json.dumps(payload, indent=2, sort_keys=True)
    record = {
        "run_id": run_id,
        "user_id": ctx.user_id,
        "username": username,
        "created_at": created_at,
        "payload_json": payload_json,
    }
    try:
        _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/experiment_runs",
            payload=record,
            extra_headers={"Prefer": "return=minimal"},
        )
    except SupabaseSchemaNotReadyError as exc:
        raise RuntimeError(
            "Supabase is connected, but the LineageTrace tables are not set up yet. "
            "Run scripts/setup_supabase_direct_auth.sql in the Supabase SQL Editor, then try again."
        ) from exc
    return run_id, Path(f"{created_at[:10]}_{username}_{run_id[:8]}.json")


def update_experiment_payload(run_id: str, payload: dict[str, Any]) -> Path:
    ctx = _require_context()
    created_at = _now_iso()
    payload_json = json.dumps(payload, indent=2, sort_keys=True)
    _rest_json(
        ctx,
        method="PATCH",
        path="/rest/v1/experiment_runs",
        query={
            "run_id": f"eq.{run_id}",
        },
        payload={
            "created_at": created_at,
            "payload_json": payload_json,
        },
        extra_headers={"Prefer": "return=minimal"},
    )
    return Path(f"{created_at[:10]}_{ctx.username}_{run_id[:8]}.json")


def list_experiment_runs(username: str) -> list[dict[str, str]]:
    ctx = _require_context()
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/experiment_runs",
            query={
                "select": "run_id,created_at,payload_json",
                "username": f"eq.{username}",
                "order": "created_at.desc",
            },
        )
    except SupabaseSchemaNotReadyError:
        return []
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "run_id": str(row.get("run_id", "")),
                "created_at": str(row.get("created_at", "")),
                "payload_json": str(row.get("payload_json", "")),
            }
        )
    return out


def get_user_storage_locations(username: str) -> list[str]:
    ctx = _require_context()
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/user_storage_locations",
            query={
                "select": "location,last_used_at",
                "user_id": f"eq.{ctx.user_id}",
                "order": "last_used_at.desc",
            },
        )
    except SupabaseSchemaNotReadyError:
        return []
    return [
        str(row.get("location", "")).strip()
        for row in rows if isinstance(row, dict) and str(row.get("location", "")).strip()
    ]


def remember_user_storage_location(username: str, location: str) -> None:
    ctx = _require_context()
    loc = location.strip()
    if not loc:
        return
    try:
        _ensure_current_user_row(ctx)
    except SupabaseSchemaNotReadyError:
        return
    now = _now_iso()
    try:
        _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/user_storage_locations",
            query={"on_conflict": "user_id,location"},
            payload={
                "user_id": ctx.user_id,
                "username": ctx.username,
                "location": loc,
                "last_used_at": now,
            },
            extra_headers={
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
    except SupabaseSchemaNotReadyError:
        return


def list_standard_objects(username: str, category: str | None = None) -> list[dict[str, Any]]:
    ctx = _require_context()
    query = {
        "select": "id,username,category,name,metadata_json,created_at,updated_at",
    }
    if category:
        query["category"] = f"eq.{category}"
        query["order"] = "name.asc"
    else:
        query["order"] = "category.asc,name.asc"
    try:
        rows = _rest_json(
            ctx,
            method="GET",
            path="/rest/v1/standards_objects",
            query=query,
        )
    except SupabaseSchemaNotReadyError:
        return []

    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        meta_raw = str(row.get("metadata_json", "{}"))
        try:
            metadata = json.loads(meta_raw)
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        out.append(
            {
                "id": int(row.get("id", 0) or 0),
                "username": str(row.get("username", "")),
                "category": str(row.get("category", "")),
                "name": str(row.get("name", "")),
                "metadata": metadata,
                "created_at": str(row.get("created_at", "")),
                "updated_at": str(row.get("updated_at", "")),
            }
        )
    return out


def upsert_standard_object(
    username: str,
    category: str,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    ctx = _require_context()
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Standard object name is required.")
    metadata = metadata or {}
    visibility = str(metadata.get("visibility", "Personal")).strip() or "Personal"
    try:
        _ensure_current_user_row(ctx)
    except SupabaseSchemaNotReadyError as exc:
        raise RuntimeError(
            "Supabase is connected, but the LineageTrace tables are not set up yet. "
            "Run scripts/setup_supabase_direct_auth.sql in the Supabase SQL Editor, then try again."
        ) from exc
    now = _now_iso()
    try:
        if visibility == "Shared":
            existing = _rest_json(
                ctx,
                method="GET",
                path="/rest/v1/standards_objects",
                query={
                    "select": "id",
                    "category": f"eq.{category}",
                    "name": f"eq.{clean_name}",
                    "visibility": "eq.Shared",
                    "limit": "1",
                },
            )
            if isinstance(existing, list) and existing:
                row_id = str(existing[0].get("id", "")).strip()
                if row_id:
                    _rest_json(
                        ctx,
                        method="PATCH",
                        path="/rest/v1/standards_objects",
                        query={"id": f"eq.{row_id}"},
                        payload={
                            "username": ctx.username,
                            "metadata_json": json.dumps(metadata, sort_keys=True),
                            "visibility": visibility,
                            "updated_at": now,
                        },
                        extra_headers={"Prefer": "return=minimal"},
                    )
                    return
        _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/standards_objects",
            query={"on_conflict": "user_id,category,name"},
            payload={
                "user_id": ctx.user_id,
                "username": ctx.username,
                "category": category,
                "name": clean_name,
                "metadata_json": json.dumps(metadata, sort_keys=True),
                "visibility": visibility,
                "created_at": now,
                "updated_at": now,
            },
            extra_headers={
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
    except SupabaseSchemaNotReadyError as exc:
        raise RuntimeError(
            "Supabase is connected, but the LineageTrace tables are not set up yet. "
            "Run scripts/setup_supabase_direct_auth.sql in the Supabase SQL Editor, then try again."
        ) from exc


def delete_standard_object(username: str, category: str, name: str) -> None:
    ctx = _require_context()
    try:
        _rest_json(
            ctx,
            method="DELETE",
            path="/rest/v1/standards_objects",
            query={
                "category": f"eq.{category}",
                "name": f"eq.{name.strip()}",
            },
            extra_headers={"Prefer": "return=minimal"},
        )
    except SupabaseSchemaNotReadyError:
        return


def backup_inventory_rows(category: str, rows: list[dict[str, Any]], visibility_filter: str = "all") -> str:
    ctx = _require_context()
    if not rows:
        return ""
    payload = {
        "user_id": ctx.user_id,
        "username": ctx.username,
        "category": category.strip(),
        "visibility_filter": visibility_filter.strip().lower() or "all",
        "row_count": len(rows),
        "snapshot_json": rows,
        "created_at": _now_iso(),
    }
    try:
        result = _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/inventory_backups",
            payload=payload,
            extra_headers={"Prefer": "return=representation"},
        )
    except SupabaseSchemaNotReadyError as exc:
        raise RuntimeError(
            "The inventory backup table is not set up yet. Re-run "
            "scripts/setup_supabase_direct_auth.sql in the Supabase SQL Editor, then try again."
        ) from exc
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return str(result[0].get("id", "")).strip()
    return ""


def delete_standard_objects_by_ids(row_ids: list[int]) -> int:
    ctx = _require_context()
    deleted = 0
    seen: set[int] = set()
    for row_id in row_ids:
        try:
            clean_id = int(row_id)
        except (TypeError, ValueError):
            continue
        if clean_id <= 0 or clean_id in seen:
            continue
        seen.add(clean_id)
        try:
            _rest_json(
                ctx,
                method="DELETE",
                path="/rest/v1/standards_objects",
                query={"id": f"eq.{clean_id}"},
                extra_headers={"Prefer": "return=minimal"},
            )
            deleted += 1
        except SupabaseSchemaNotReadyError:
            return deleted
    return deleted


class _Context:
    def __init__(
        self,
        base_url: str,
        anon_key: str,
        access_token: str,
        user_id: str,
        username: str,
        refresh_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.access_token = access_token
        self.user_id = user_id
        self.username = username
        self.refresh_token = refresh_token


def _context_from_access_token(access_token: str) -> _Context:
    connection = load_connection_settings()
    clean_access_token = access_token.strip()
    if connection is None or connection.mode != "supabase" or not clean_access_token:
        raise RuntimeError("Supabase connection settings are not configured.")
    claims = _decode_jwt_payload(clean_access_token)
    user_id = str(claims.get("sub", "")).strip()
    email = str(claims.get("email", "")).strip()
    if not user_id or not email:
        raise RuntimeError("Could not determine the signed-in Supabase user.")
    return _Context(connection.server_url, connection.anon_key, clean_access_token, user_id, email)


def _require_context() -> _Context:
    connection = load_connection_settings()
    session = load_session_settings()
    if connection is None or connection.mode != "supabase":
        raise RuntimeError("Supabase connection settings are not configured.")
    if session is None or not session.access_token:
        raise RuntimeError("No Supabase session is available. Sign in again and retry.")
    claims = _decode_jwt_payload(session.access_token)
    user_id = str(claims.get("sub", "")).strip()
    username = session.username or str(claims.get("email", "")).strip()
    if not user_id or not username:
        raise RuntimeError("Could not determine the signed-in Supabase user.")
    return _Context(
        base_url=connection.server_url,
        anon_key=connection.anon_key,
        access_token=session.access_token,
        user_id=user_id,
        username=username,
        refresh_token=session.refresh_token,
    )


def _display_name_from_email(email: str) -> str:
    token = email.strip().split("@", 1)[0].replace(".", " ").replace("_", " ")
    return token.title() if token else ""


def _email_for_user_id(ctx: _Context, user_id: str) -> str:
    clean_id = user_id.strip()
    if not clean_id:
        return ""
    rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/app_users",
        query={
            "select": "email",
            "user_id": f"eq.{clean_id}",
            "limit": "1",
        },
    )
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("email", "")).strip()
    return ""


def _review_access_request(
    request_id: str,
    *,
    action: str,
    role: str = "",
    display_name: str = "",
    review_note: str = "",
) -> None:
    ctx = _require_context()
    clean_id = request_id.strip()
    if action not in {"approved", "denied"} or not clean_id:
        raise ValueError("Invalid access request review.")
    rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/access_requests",
        query={
            "select": "id,email,display_name,requested_role",
            "id": f"eq.{clean_id}",
            "limit": "1",
        },
    )
    if not isinstance(rows, list) or not rows:
        raise ValueError("Access request was not found.")
    row = rows[0]
    if not isinstance(row, dict):
        raise ValueError("Access request was not found.")
    requested_role = str(row.get("requested_role", "")).strip().lower()
    approved_role = role.strip().lower() or requested_role
    if action == "approved" and approved_role not in {"admin", "researcher"}:
        raise ValueError("Approved role must be admin or researcher.")
    email = str(row.get("email", "")).strip()
    approved_display_name = display_name.strip() or str(row.get("display_name", "")).strip() or _display_name_from_email(email)
    now = _now_iso()
    if action == "approved":
        _rest_json(
            ctx,
            method="POST",
            path="/rest/v1/authorized_users",
            query={"on_conflict": "email"},
            payload={
                "email": email,
                "role": approved_role,
                "display_name": approved_display_name,
                "active": True,
                "updated_at": now,
            },
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
    _rest_json(
        ctx,
        method="PATCH",
        path="/rest/v1/access_requests",
        query={"id": f"eq.{clean_id}"},
        payload={
            "status": action,
            "reviewed_at": now,
            "reviewed_by_user_id": ctx.user_id,
            "review_note": review_note.strip(),
        },
        extra_headers={"Prefer": "return=minimal"},
    )


def _refresh_context_session(ctx: _Context) -> _Context:
    refresh_token = ctx.refresh_token.strip()
    if not refresh_token:
        clear_session_settings()
        raise SupabaseAuthExpiredError("Supabase session expired. Please sign in again.")
    url = f"{ctx.base_url}/auth/v1/token?grant_type=refresh_token"
    headers = {
        "apikey": ctx.anon_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "LineageTraceBeta/0.1",
    }
    request = Request(
        url,
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        clear_session_settings()
        detail = exc.read().decode("utf-8", errors="replace")
        raise SupabaseAuthExpiredError(f"Supabase session refresh failed: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Supabase at {ctx.base_url}.") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        clear_session_settings()
        raise SupabaseAuthExpiredError("Supabase session refresh returned an invalid response.") from exc
    access_token = str(data.get("access_token", "")).strip()
    new_refresh_token = str(data.get("refresh_token", "")).strip() or refresh_token
    if not access_token:
        clear_session_settings()
        raise SupabaseAuthExpiredError("Supabase session refresh did not return an access token.")
    claims = _decode_jwt_payload(access_token)
    user_id = str(claims.get("sub", "")).strip() or ctx.user_id
    username = str(claims.get("email", "")).strip() or ctx.username
    existing = load_session_settings()
    save_session_settings(
        SessionSettings(
            connection_mode="supabase",
            server_url=ctx.base_url,
            username=username,
            display_name=(existing.display_name if existing else ""),
            role=(existing.role if existing else "researcher"),
            access_token=access_token,
            refresh_token=new_refresh_token,
        )
    )
    return _Context(ctx.base_url, ctx.anon_key, access_token, user_id, username, new_refresh_token)


def _ensure_current_user_row(ctx: _Context, role: str = "") -> None:
    clean_role = role.strip() or _current_authorized_user(ctx)["role"]
    _rest_json(
        ctx,
        method="POST",
        path="/rest/v1/app_users",
        query={"on_conflict": "email"},
        payload={
            "user_id": ctx.user_id,
            "email": ctx.username,
            "role": clean_role,
            "updated_at": _now_iso(),
        },
        extra_headers={
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )


def _lookup_authorized_user(ctx: _Context) -> dict[str, str] | None:
    rows = _rest_json(
        ctx,
        method="GET",
        path="/rest/v1/authorized_users",
        query={
            "select": "role,display_name",
            "email": f"ilike.{ctx.username}",
            "active": "eq.true",
            "limit": "1",
        },
    )
    if isinstance(rows, list) and rows:
        role = str(rows[0].get("role", "")).strip()
        display_name = str(rows[0].get("display_name", "")).strip()
        return {
            "role": role or "researcher",
            "display_name": display_name,
        }
    return None


def _current_authorized_user(ctx: _Context) -> dict[str, str]:
    profile = _lookup_authorized_user(ctx)
    if profile is not None:
        return profile
    return {"role": "researcher", "display_name": ""}


def _current_user_role(ctx: _Context) -> str:
    return _current_authorized_user(ctx)["role"]


def _rest_json(
    ctx: _Context,
    *,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    return _rest_json_once(
        ctx,
        method=method,
        path=path,
        query=query,
        payload=payload,
        extra_headers=extra_headers,
        allow_refresh=True,
    )


def _rest_json_once(
    ctx: _Context,
    *,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    allow_refresh: bool = True,
) -> Any:
    url = f"{ctx.base_url}{path}"
    if query:
        url = f"{url}?{urlencode(query, quote_via=quote)}"
    headers = {
        "apikey": ctx.anon_key,
        "Authorization": f"Bearer {ctx.access_token}",
        "Accept": "application/json",
        "User-Agent": "LineageTraceBeta/0.1",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = Request(
        url,
        data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401 and ("JWT expired" in body or '"code":"PGRST303"' in body):
            if allow_refresh:
                refreshed_ctx = _refresh_context_session(ctx)
                return _rest_json_once(
                    refreshed_ctx,
                    method=method,
                    path=path,
                    query=query,
                    payload=payload,
                    extra_headers=extra_headers,
                    allow_refresh=False,
                )
            clear_session_settings()
            raise SupabaseAuthExpiredError("Supabase session expired. Please sign in again.") from exc
        if '"code":"PGRST205"' in body or "schema cache" in body:
            raise SupabaseSchemaNotReadyError(body) from exc
        raise RuntimeError(f"Supabase request failed ({exc.code}): {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Supabase at {ctx.base_url}.") from exc
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _decode_jwt_payload(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
