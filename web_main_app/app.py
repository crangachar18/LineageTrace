from __future__ import annotations

import json
import logging
import os
import csv
import io
import secrets
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional local convenience dependency
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

logger = logging.getLogger("lineagetrace.web")

from pyapp.database import (
    clear_experiment_runs,
    get_user_storage_locations,
    delete_standard_object,
    init_auth_db,
    list_experiment_runs,
    list_standard_objects,
    remember_user_storage_location,
    save_experiment_payload,
    update_experiment_payload,
    upsert_standard_object,
    verify_credentials,
)
from pyapp.antibody_rules import load_primaries_from_inventory
from pyapp.app_config import (
    SessionSettings,
    clear_session_settings,
    get_authorized_login_emails,
    get_display_name_overrides,
    load_connection_settings,
    load_session_settings,
)
from pyapp.secondary_rules import load_secondaries_from_inventory
from pyapp.supabase_direct_database import (
    SupabaseAuthExpiredError,
    SupabaseSchemaNotReadyError,
    approve_access_request,
    backup_inventory_rows,
    current_session_settings,
    deny_access_request,
    delete_standard_objects_by_ids,
    establish_session,
    get_supervisor_status,
    list_access_requests,
    list_available_admins,
    list_supervisor_requests,
    list_visible_record_owners,
    review_supervisor_request,
    set_request_session_settings,
    submit_access_request,
    submit_supervisor_request,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="LineageTrace Web")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("LINEAGETRACE_WEB_SECRET", secrets.token_urlsafe(32)),
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(SupabaseAuthExpiredError)
async def _supabase_auth_expired_handler(request: Request, exc: SupabaseAuthExpiredError):
    request.session.clear()
    clear_session_settings()
    return _redirect("/")


def _browser_supabase_session(request: Request) -> SessionSettings | None:
    access_token = str(request.session.get("supabase_access_token", "")).strip()
    if not access_token:
        return None
    connection = load_connection_settings()
    return SessionSettings(
        connection_mode="supabase",
        server_url=(connection.server_url if connection else str(request.session.get("supabase_server_url", ""))),
        username=str(request.session.get("username", "")).strip(),
        display_name=str(request.session.get("display_name", "")).strip(),
        role=str(request.session.get("role", "")).strip() or "researcher",
        access_token=access_token,
        refresh_token=str(request.session.get("supabase_refresh_token", "")).strip(),
    )


def _store_browser_supabase_session(request: Request, settings: SessionSettings) -> None:
    request.session["username"] = settings.username
    request.session["role"] = settings.role
    request.session["display_name"] = settings.display_name
    request.session["supabase_server_url"] = settings.server_url
    request.session["supabase_access_token"] = settings.access_token
    request.session["supabase_refresh_token"] = settings.refresh_token


EXPERIMENT_TYPES: list[str] = [
    "Immunohistochemistry (IHC)",
    "Polymerase Chain Reaction (PCR)",
]

INVENTORY_CATEGORIES = [
    "pcr_primer",
    "primary_antibody",
    "secondary_antibody",
    "pcr_amplicon",
    "slibrary",
]

INVENTORY_CATEGORY_LABELS = {
    "primary_antibody": "Primaries",
    "secondary_antibody": "Secondaries",
    "pcr_primer": "PCR Primers",
    "pcr_amplicon": "PCR Products",
    "slibrary": "Slides",
}

INVENTORY_CATEGORY_COLUMNS = {
    "primary_antibody": [
        ("name", "Name"),
        ("researcher_name", "Researcher Name"),
        ("animal_raised_in", "Raised In"),
        ("antigen", "Antigen"),
        ("igg_subtype", "IgG Subtype"),
        ("catalog_number", "Catalog #"),
        ("standard_dilution", "Std Dilution"),
        ("storage_location", "Storage"),
        ("visibility", "Visibility"),
    ],
    "secondary_antibody": [
        ("name", "Name"),
        ("researcher_name", "Researcher Name"),
        ("animal_raised_in", "Raised In"),
        ("animal_raised_against", "Raised Against"),
        ("fluorophore", "Fluor"),
        ("igg_subtype", "IgG Subtype"),
        ("standard_dilution", "Std Dilution"),
        ("visibility", "Visibility"),
    ],
    "pcr_primer": [
        ("name", "Name"),
        ("researcher_name", "Researcher Name"),
        ("sequence", "Sequence"),
        ("orientation", "Orientation"),
        ("aliquot", "Aliquot"),
        ("dilution_factor_nx", "Dilution Factor (nx)"),
        ("nmol", "nmol"),
        ("received", "Received"),
        ("visibility", "Visibility"),
    ],
    "pcr_amplicon": [
        ("name", "Name"),
        ("researcher_name", "Researcher Name"),
        ("length", "Length"),
        ("concentration", "Concentration"),
        ("disposition", "Disposition"),
        ("storage_location", "Storage"),
        ("a_tail", "A-tail"),
        ("mod_5p", "5' Mod"),
        ("mod_3p", "3' Mod"),
        ("visibility", "Visibility"),
    ],
    "slibrary": [
        ("name", "Slide ID"),
        ("researcher_name", "Researcher Name"),
        ("group_name", "Group"),
        ("primary_set", "Primary Set"),
        ("primary_mm", "Primary MM"),
        ("secondary_set", "Secondary Set"),
        ("secondary_mm", "Secondary MM"),
        ("edu_enabled", "EdU"),
        ("disposition", "Disposition"),
        ("imaging", "Imaging"),
        ("storage_location", "Storage"),
        ("planned_use", "Planned Use"),
        ("visibility", "Visibility"),
    ],
}

INVENTORY_SECTIONS = [
    ("reagents", "Reagents", ["primary_antibody", "secondary_antibody", "pcr_primer"]),
    ("products", "Products", ["pcr_amplicon", "slibrary"]),
]

RESEARCHER_EDITABLE_PRODUCT_CATEGORIES = {"pcr_amplicon", "slibrary"}
PCR_PRODUCT_DISPOSITIONS = {"gel purified", "pcr purified", "stored", "discarded"}
SLIDE_DISPOSITIONS = {"stored", "discarded"}

INVENTORY_SCHEMA_PATH = BASE_DIR / "inventory_schema.json"

BETA_RECORD_SUBORDINATES: dict[str, list[str]] = {}


@app.on_event("startup")
def _startup() -> None:
    init_auth_db()


def _current_user(request: Request) -> dict[str, str] | None:
    username = str(request.session.get("username", "")).strip()
    role = str(request.session.get("role", "")).strip()
    display_name = str(request.session.get("display_name", "")).strip()
    if not username:
        return None
    connection = load_connection_settings()
    if connection is not None and connection.mode == "supabase":
        access_token = str(request.session.get("supabase_access_token", "")).strip()
        if not access_token:
            return None
    return {"username": username, "display_name": display_name or _fallback_display_name(username), "role": role or "researcher"}


def _activate_browser_supabase_session(request: Request) -> None:
    set_request_session_settings(_browser_supabase_session(request))


def _require_user(request: Request) -> dict[str, str]:
    _activate_browser_supabase_session(request)
    user = _current_user(request)
    if user is None:
        raise PermissionError
    return user


def _is_inventory_admin(role: str) -> bool:
    return role in {"main_admin", "admin"}


def _supabase_connection_ready() -> bool:
    connection = load_connection_settings()
    return connection is not None and connection.mode == "supabase" and bool(connection.server_url and connection.anon_key)


def _is_authorized_login_email(email: str) -> bool:
    authorized = get_authorized_login_emails()
    if not authorized:
        return True
    return email.strip().lower() in authorized


def _unauthorized_login_response(request: Request, email: str) -> HTMLResponse:
    request.session.clear()
    clear_session_settings()
    return templates.TemplateResponse(
        request,
        "login.html",
        _template_context(
            request,
            error=f"{email.strip() or 'This email'} is not authorized for this LineageTrace deployment.",
        ),
        status_code=403,
    )


def _access_request_response(
    request: Request,
    *,
    email: str,
    access_token: str,
    error: str = "",
) -> HTMLResponse:
    request.session.clear()
    clear_session_settings()
    return templates.TemplateResponse(
        request,
        "access_request.html",
        _template_context(
            request,
            email=email.strip(),
            display_name=_fallback_display_name(email),
            access_token=access_token,
            error=error,
        ),
        status_code=(400 if error else 200),
    )


def _researcher_needs_supervisor_setup(role: str) -> bool:
    if role != "researcher" or not _supabase_connection_ready():
        return False
    try:
        status = get_supervisor_status()
    except Exception:
        return False
    return status.get("state") in {"unassigned", "denied", "canceled", "not_configured"}


def _record_owner_options(user: dict[str, str]) -> list[dict[str, str]]:
    username = user["username"]
    owners = [username]
    if _supabase_connection_ready() and _is_inventory_admin(user["role"]):
        try:
            role_filter = "" if user["role"] == "main_admin" else "researcher"
            visible_owners = [
                item["email"]
                for item in list_visible_record_owners(role_filter)
                if item.get("email") and item.get("email") != username
            ]
            owners.extend(visible_owners)
        except Exception:
            owners.extend(BETA_RECORD_SUBORDINATES.get(username, []))
    elif _is_inventory_admin(user["role"]):
        owners.extend(BETA_RECORD_SUBORDINATES.get(username, []))
    deduped: list[str] = []
    seen: set[str] = set()
    for owner in owners:
        clean_owner = owner.strip()
        if not clean_owner or clean_owner.lower() in seen:
            continue
        deduped.append(clean_owner)
        seen.add(clean_owner.lower())
    return [
        {
            "username": owner,
            "label": f"{_display_researcher_name(owner)} (You)" if owner == username else _display_researcher_name(owner),
        }
        for owner in deduped
    ]


def _selected_record_owners(request: Request, user: dict[str, str]) -> list[str]:
    allowed = [item["username"] for item in _record_owner_options(user)]
    stored = request.session.get("record_owner_filter")
    if not isinstance(stored, list):
        return [user["username"]]
    selected = [str(item).strip() for item in stored if str(item).strip() in allowed]
    return selected or [user["username"]]


def _load_runs_for_owners(owners: list[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for owner in owners:
        runs.extend(_load_runs(owner))
    return runs


def _find_accessible_run(request: Request, user: dict[str, str], run_id: str) -> tuple[dict[str, Any], str] | None:
    clean_run_id = run_id.strip()
    if not clean_run_id:
        return None
    for owner in [item["username"] for item in _record_owner_options(user)]:
        found = _find_run(owner, clean_run_id)
        if found is not None:
            return found, owner
    return None


def _redirect(location: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=status_code)


def _normalize_experiment_type(experiment_type: str) -> tuple[str, str]:
    clean = experiment_type.strip()
    lowered = clean.lower()
    if lowered in {"immunohistochemistry (ihc)", "ihc", "immunohistochemistry"}:
        return "Immunohistochemistry (IHC)", "IHC"
    if lowered in {"polymerase chain reaction (pcr)", "pcr", "polymerase chain reaction"}:
        return "Polymerase Chain Reaction (PCR)", "PCR"
    return clean, clean


def _run_name_field(protocol_code: str) -> str:
    if protocol_code == "IHC":
        return "ihc_run_name"
    if protocol_code == "PCR":
        return "pcr_run_name"
    return "run_name"


def _parse_payload_json(payload_text: str) -> dict[str, Any]:
    raw = payload_text.strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Custom payload JSON must be an object.")
    return parsed


def _load_runs(username: str) -> list[dict[str, Any]]:
    rows = list_experiment_runs(username)
    out: list[dict[str, Any]] = []
    for row in rows:
        payload_raw = str(row.get("payload_json", ""))
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        exp_type = str(payload.get("experiment_type", "")).strip()
        if not exp_type and str(payload.get("stage", "")).strip().lower() == "final_slide_book":
            exp_type = "IHC"
        run_name = (
            str(payload.get("ihc_run_name", "")).strip()
            or str(payload.get("pcr_run_name", "")).strip()
            or str(payload.get("facs_run_name", "")).strip()
            or str(payload.get("run_name", "")).strip()
            or str(payload.get("name", "")).strip()
            or row.get("run_id", "")
        )
        out.append(
            {
                "run_id": str(row.get("run_id", "")),
                "created_at": str(row.get("created_at", "")),
                "experiment_type": exp_type or "Unknown",
                "run_name": run_name,
                "researcher_user_id": str(payload.get("researcher_user_id", "")).strip() or _researcher_user_id(username),
                "researcher_name": str(payload.get("researcher_name", "")).strip() or _display_researcher_name(username),
                "notes": str(payload.get("notes", "")).strip(),
                "status": str(payload.get("record_status", "Submitted")).strip() or "Submitted",
                "started_record_on": str(payload.get("started_record_on", row.get("created_at", ""))).strip(),
                "last_closed": str(payload.get("last_closed", row.get("created_at", ""))).strip(),
                "payload": payload,
                "payload_pretty": json.dumps(payload, indent=2, sort_keys=True),
            }
        )
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_run(username: str, run_id: str) -> dict[str, Any] | None:
    for run in _load_runs(username):
        if run["run_id"] == run_id:
            return run
    return None


def _records_metrics(runs: list[dict[str, Any]]) -> dict[str, int]:
    drafts = sum(1 for run in runs if run["status"].lower() == "draft")
    submitted = sum(1 for run in runs if run["status"].lower() == "submitted")
    return {"drafts": drafts, "submitted": submitted}


def _filtered_records(runs: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    normalized = scope.strip().lower() or "saved"
    if normalized == "drafts":
        return [run for run in runs if run["status"].lower() == "draft"]
    if normalized == "all":
        return runs
    return [run for run in runs if run["status"].lower() == "submitted"]


def _sort_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        runs,
        key=lambda run: (run.get("last_closed", ""), run.get("started_record_on", "")),
        reverse=True,
    )


def _inventory_rows(username: str, category: str | None) -> list[dict[str, Any]]:
    rows = list_standard_objects(username, category)
    for row in rows:
        row["metadata_pretty"] = json.dumps(row.get("metadata", {}), indent=2, sort_keys=True)
    return rows


def _fallback_display_name(username: str) -> str:
    clean = username.strip()
    if "@" in clean:
        clean = clean.split("@", 1)[0]
    token = clean.replace("_", " ").replace(".", " ")
    return token.title() if token else ""


def _display_name_override(identifier: str) -> str:
    return get_display_name_overrides().get(identifier.strip().lower(), "")


def _display_researcher_name(username: str) -> str:
    override = _display_name_override(username)
    if override:
        return override
    current_session = current_session_settings() or load_session_settings()
    if current_session is not None and username.strip().lower() == current_session.username.strip().lower():
        if current_session.display_name.strip():
            return current_session.display_name.strip()
    token = username.strip().replace("_", " ")
    return _fallback_display_name(token)


def _researcher_user_id(username: str) -> str:
    return username.strip()


def _find_standard_object(username: str, category: str, name: str) -> dict[str, Any] | None:
    clean_name = name.strip()
    if not clean_name:
        return None
    for row in list_standard_objects(username, category):
        if str(row.get("name", "")).strip() == clean_name:
            return row
    return None


def _normalize_inventory_field_key(raw: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    return key


def _load_inventory_schema() -> dict[str, list[dict[str, str]]]:
    if not INVENTORY_SCHEMA_PATH.exists():
        return {}
    try:
        parsed = json.loads(INVENTORY_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    normalized: dict[str, list[dict[str, str]]] = {}
    for category, columns in parsed.items():
        if not isinstance(category, str) or not isinstance(columns, list):
            continue
        out_columns: list[dict[str, str]] = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            field = str(column.get("field", "")).strip()
            label = str(column.get("label", "")).strip()
            if not field or not label:
                continue
            out_columns.append({"field": field, "label": label})
        if out_columns:
            normalized[category] = out_columns
    return normalized


def _save_inventory_schema(schema: dict[str, list[dict[str, str]]]) -> None:
    INVENTORY_SCHEMA_PATH.write_text(
        json.dumps(schema, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _inventory_fields(category: str) -> list[dict[str, str]]:
    base_fields = [
        {"field": field, "label": label}
        for field, label in INVENTORY_CATEGORY_COLUMNS.get(category, [("name", "Name")])
    ]
    seen = {item["field"] for item in base_fields}
    for extra in _load_inventory_schema().get(category, []):
        if extra["field"] in seen:
            continue
        base_fields.append(extra)
        seen.add(extra["field"])
    return base_fields


def _normalize_researcher_name_for_display(value: str, username: str) -> str:
    clean_value = value.strip()
    clean_username = username.strip()
    if not clean_value:
        return _display_researcher_name(clean_username)
    override = _display_name_override(clean_value)
    if override:
        return override
    if "@" in clean_value and clean_value.lower() == clean_username.lower():
        return _display_researcher_name(clean_username)
    return clean_value


def _inventory_table_rows(username: str, role: str, category: str, visibility_filter: str = "all") -> dict[str, Any]:
    raw_rows = _inventory_rows(username, category)
    fields = _inventory_fields(category)
    selected_visibility = visibility_filter.strip().lower() or "all"
    table_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        row_visibility = str(metadata.get("visibility", "")).strip() or "Shared"
        if selected_visibility in {"shared", "personal"} and row_visibility.lower() != selected_visibility:
            continue
        is_admin = _is_inventory_admin(role)
        is_own_personal_product = (
            role == "researcher"
            and category in RESEARCHER_EDITABLE_PRODUCT_CATEGORIES
            and row_visibility.lower() == "personal"
            and str(row.get("username", "")).strip().lower() == username.strip().lower()
        )
        values: dict[str, str] = {}
        for item in fields:
            field = item["field"]
            if field == "name":
                values[field] = str(row.get("name", "")).strip()
            elif field == "researcher_name":
                values[field] = _normalize_researcher_name_for_display(
                    str(metadata.get(field, "")).strip(),
                    username,
                )
            else:
                values[field] = str(metadata.get(field, "")).strip()
        table_rows.append(
            {
                "original_name": str(row.get("name", "")).strip(),
                "cells": [values[item["field"]] for item in fields],
                "form_values": values,
                "can_edit": is_admin or is_own_personal_product,
                "can_delete": is_admin,
            }
        )
    can_show_actions = _is_inventory_admin(role) or any(row["can_edit"] or row["can_delete"] for row in table_rows)
    return {
        "category": category,
        "label": INVENTORY_CATEGORY_LABELS.get(category, category),
        "fields": fields,
        "columns": [item["label"] for item in fields],
        "rows": table_rows,
        "count": len(table_rows),
        "can_admin_edit": _is_inventory_admin(role),
        "can_show_actions": can_show_actions,
        "can_add_rows": True,
    }


def _inventory_page_sections(username: str, role: str, visibility_filter: str = "all") -> list[dict[str, Any]]:
    return [
        {
            "key": section_key,
            "label": section_label,
            "categories": [
                _inventory_table_rows(username, role, item_category, visibility_filter)
                for item_category in section_categories
            ],
        }
        for section_key, section_label, section_categories in INVENTORY_SECTIONS
    ]


def _inventory_field_aliases(category: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for field in _inventory_fields(category):
        field_key = field["field"]
        aliases[_normalize_inventory_field_key(field_key)] = field_key
        aliases[_normalize_inventory_field_key(field["label"])] = field_key
    return aliases


def _filter_inventory_rows_for_visibility(rows: list[dict[str, Any]], visibility_filter: str) -> list[dict[str, Any]]:
    selected_visibility = _normalize_inventory_visibility_filter(visibility_filter)
    if selected_visibility == "all":
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        row_visibility = str(metadata.get("visibility", "")).strip() or "Shared"
        if row_visibility.lower() == selected_visibility:
            filtered.append(row)
    return filtered


def _coerce_inventory_metadata(
    user: dict[str, str],
    category: str,
    clean_name: str,
    field_values: dict[str, Any],
    *,
    default_visibility: str = "",
    original_name: str = "",
) -> dict[str, Any]:
    existing_row = (
        _find_standard_object(user["username"], category, original_name)
        if original_name.strip()
        else None
    ) or _find_standard_object(user["username"], category, clean_name)
    existing_metadata = existing_row.get("metadata", {}) if existing_row else {}
    if not isinstance(existing_metadata, dict):
        existing_metadata = {}
    metadata = {
        key: value
        for key, value in field_values.items()
        if key not in {"name", "researcher_name"} and str(value).strip() != ""
    }
    metadata["researcher_user_id"] = str(existing_metadata.get("researcher_user_id", "")).strip() or _researcher_user_id(user["username"])
    metadata["researcher_name"] = _normalize_researcher_name_for_display(
        str(existing_metadata.get("researcher_name", "")).strip(),
        user["username"],
    )
    if category == "slibrary":
        imaging = str(field_values.get("imaging", "")).strip()
        if imaging:
            metadata["imaging"] = imaging
        else:
            metadata["imaging"] = str(existing_metadata.get("imaging", "")).strip() or "pending"
    if not _is_inventory_admin(user["role"]):
        metadata["visibility"] = "Personal"
    else:
        requested_visibility = str(field_values.get("visibility", "")).strip() or default_visibility.strip()
        metadata["visibility"] = requested_visibility or "Shared"
    return metadata


def _clear_generated_pcr_products(username: str, run_id: str) -> None:
    if not run_id:
        return
    for row in list_standard_objects(username, "pcr_amplicon"):
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("source_run_id", "")).strip() != run_id:
            continue
        name = str(row.get("name", "")).strip()
        if name:
            delete_standard_object(username, "pcr_amplicon", name)


def _sync_generated_pcr_products(username: str, run_id: str, payload: dict[str, Any]) -> None:
    _clear_generated_pcr_products(username, run_id)
    if str(payload.get("record_status", "")).strip().lower() != "submitted":
        return

    run_name = str(payload.get("pcr_run_name", "")).strip() or run_id[:8]
    tubes = payload.get("tubes", [])
    if not isinstance(tubes, list):
        return

    for idx, tube in enumerate(tubes, start=1):
        if not isinstance(tube, dict):
            continue
        tube_number = int(tube.get("tube") or idx)
        dna_identity = str(tube.get("dna_identity", "")).strip()
        forward_primer = str(tube.get("forward_primer", "")).strip()
        reverse_primer = str(tube.get("reverse_primer", "")).strip()
        note = str(tube.get("note", "")).strip()
        if not any([dna_identity, forward_primer, reverse_primer, note]):
            continue
        disposition = str(tube.get("disposition", "")).strip().lower()
        if disposition not in PCR_PRODUCT_DISPOSITIONS:
            disposition = "stored"
        product_name = f"{run_name} - T{tube_number}"
        upsert_standard_object(
            username,
            "pcr_amplicon",
            product_name,
            {
                "length": str(tube.get("length", "")).strip(),
                "concentration": str(tube.get("concentration", "")).strip(),
                "disposition": disposition,
                "storage_location": str(tube.get("storage_location", "")).strip(),
                "a_tail": "Yes" if tube.get("atail_enabled") else "No",
                "mod_5p": str(tube.get("mod_5p", "")).strip(),
                "mod_3p": str(tube.get("mod_3p", "")).strip(),
                "researcher_user_id": _researcher_user_id(username),
                "researcher_name": _display_researcher_name(username),
                "dna_identity": dna_identity,
                "forward_primer": forward_primer,
                "reverse_primer": reverse_primer,
                "note": note,
                "source_run_id": run_id,
                "source_run_name": run_name,
                "visibility": "Personal",
            },
        )


def _ihc_primaries(username: str) -> list[dict[str, Any]]:
    rows = []
    for primary in load_primaries_from_inventory(username):
        rows.append(
            {
                "name": primary.name,
                "animal": primary.animal,
                "catalog_number": primary.catalog_number,
                "igg_subtype": primary.igg_subtype,
                "concentration": primary.concentration,
                "concentration_text": primary.concentration_text,
            }
        )
    return rows


def _ihc_secondaries(username: str) -> list[dict[str, Any]]:
    rows = []
    for secondary in load_secondaries_from_inventory(username):
        rows.append(
            {
                "name": secondary.name,
                "concentration_text": secondary.concentration_text,
                "raised_in": secondary.raised_in,
                "anti": secondary.anti,
                "fluorophore": secondary.fluorophore,
                "mouse_subtype": secondary.mouse_subtype,
            }
        )
    return rows


def _pcr_primers(username: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in list_standard_objects(username, "pcr_primer"):
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        rows.append(
            {
                "name": str(row.get("name", "")).strip(),
                "orientation": str(metadata.get("orientation", "")).strip().upper(),
                "aliquot": str(metadata.get("aliquot", "")).strip(),
                "dilution_factor_nx": str(metadata.get("dilution_factor_nx", "")).strip(),
            }
        )
    return rows


def _template_context(request: Request, **kwargs: Any) -> dict[str, Any]:
    return {
        "request": request,
        "current_user": _current_user(request),
        "inventory_category_labels": INVENTORY_CATEGORY_LABELS,
        "supabase_oauth_enabled": _supabase_connection_ready(),
        **kwargs,
    }


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if _current_user(request) is not None:
        return _redirect("/dashboard")
    return templates.TemplateResponse(
        request,
        "login.html",
        _template_context(request, error=""),
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    clean_username = username.strip()
    if not _is_authorized_login_email(clean_username):
        return _unauthorized_login_response(request, clean_username)
    role = verify_credentials(clean_username, password)
    if role is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(
                request,
                error="Invalid username or password.",
                username=clean_username,
            ),
            status_code=400,
        )
    request.session["username"] = clean_username
    request.session["role"] = role
    saved_session = load_session_settings()
    request.session["display_name"] = saved_session.display_name if saved_session else ""
    if _researcher_needs_supervisor_setup(role):
        return _redirect("/supervisor/setup")
    return _redirect("/dashboard")


@app.get("/login/google")
def google_login(request: Request):
    connection = load_connection_settings()
    if connection is None or connection.mode != "supabase" or not connection.server_url or not connection.anon_key:
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(
                request,
                error="Supabase is not configured. Set SLIDEAPP_CONNECTION_MODE=supabase, SUPABASE_URL, and SUPABASE_ANON_KEY.",
            ),
            status_code=400,
        )
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    callback_url = str(request.url_for("auth_callback"))
    separator = "&" if "?" in callback_url else "?"
    redirect_to = f"{callback_url}{separator}{urlencode({'lt_state': state})}"
    query = urlencode(
        {
            "provider": "google",
            "redirect_to": redirect_to,
        }
    )
    return _redirect(f"{connection.server_url.rstrip('/')}/auth/v1/authorize?{query}", status_code=303)


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(request: Request):
    return templates.TemplateResponse(
        request,
        "auth_callback.html",
        _template_context(request),
    )


@app.post("/auth/session")
def auth_session(
    request: Request,
    access_token: str = Form(...),
    refresh_token: str = Form(""),
    state: str = Form(""),
):
    expected_state = str(request.session.get("oauth_state", "")).strip()
    if expected_state and state.strip() and state.strip() != expected_state:
        request.session.pop("oauth_state", None)
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(request, error="Google sign-in state did not match. Please try again."),
            status_code=400,
        )
    request.session.pop("oauth_state", None)
    from pyapp.supabase_direct_database import _decode_jwt_payload

    claims = _decode_jwt_payload(access_token)
    username = str(claims.get("email", "")).strip()
    try:
        role = establish_session(access_token, refresh_token)
    except SupabaseSchemaNotReadyError as exc:
        logger.exception("Supabase schema check failed for %s", username or "<unknown>")
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(
                request,
                error=(
                    "Supabase sign-in worked, but Supabase REST could not see one of the LineageTrace tables. "
                    f"Server detail: {exc}"
                ),
            ),
            status_code=503,
        )
    except RuntimeError as exc:
        logger.exception("Supabase session establishment failed for %s", username or "<unknown>")
        if "not approved" not in str(exc).lower():
            return templates.TemplateResponse(
                request,
                "login.html",
                _template_context(
                    request,
                    error=(
                        "Supabase sign-in worked, but LineageTrace could not finish the app session. "
                        f"Server detail: {exc}"
                    ),
                ),
                status_code=500,
            )
        return _access_request_response(
            request,
            email=username,
            access_token=access_token,
            error=(
                "This Google account is not approved for LineageTrace yet. "
                "Submit an access request for main admin review."
            ),
        )
    if role is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(request, error="Could not establish a Supabase session."),
            status_code=400,
        )
    # The Supabase session helper persists the email as its username. Decode it
    # through the same JWT claims to keep the browser session aligned.
    saved_session = current_session_settings() or load_session_settings()
    request.session["username"] = username
    request.session["role"] = role
    request.session["display_name"] = saved_session.display_name if saved_session else ""
    if saved_session is not None and saved_session.access_token:
        _store_browser_supabase_session(request, saved_session)
    if _researcher_needs_supervisor_setup(role):
        return _redirect("/supervisor/setup")
    return _redirect("/dashboard")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    clear_session_settings()
    return _redirect("/")


@app.post("/access/request", response_class=HTMLResponse)
def access_request_submit(
    request: Request,
    access_token: str = Form(...),
    display_name: str = Form(...),
    requested_role: str = Form(...),
    request_note: str = Form(""),
):
    from pyapp.supabase_direct_database import _decode_jwt_payload

    claims = _decode_jwt_payload(access_token)
    email = str(claims.get("email", "")).strip()
    if not email:
        return templates.TemplateResponse(
            request,
            "login.html",
            _template_context(request, error="Could not read the Google account email. Please try signing in again."),
            status_code=400,
        )
    try:
        submit_access_request(
            access_token,
            requested_role=requested_role,
            display_name=display_name,
            request_note=request_note,
        )
    except Exception as exc:
        return _access_request_response(request, email=email, access_token=access_token, error=str(exc))
    request.session.clear()
    clear_session_settings()
    return templates.TemplateResponse(
        request,
        "access_request_submitted.html",
        _template_context(request, email=email),
    )


@app.get("/access-requests", response_class=HTMLResponse)
def access_requests_page(request: Request, status: str = "pending"):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] != "main_admin":
        return _redirect("/dashboard")
    clean_status = status.strip().lower()
    if clean_status not in {"pending", "approved", "denied", "all"}:
        clean_status = "pending"
    requests = list_access_requests("" if clean_status == "all" else clean_status)
    return templates.TemplateResponse(
        request,
        "access_requests.html",
        _template_context(request, access_requests=requests, status=clean_status, error=""),
    )


@app.post("/access-requests/review", response_class=HTMLResponse)
def access_request_review(
    request: Request,
    request_id: str = Form(...),
    action: str = Form(...),
    role: str = Form(""),
    display_name: str = Form(""),
    review_note: str = Form(""),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] != "main_admin":
        return _redirect("/dashboard")
    try:
        if action.strip().lower() == "approve":
            approve_access_request(request_id, role, display_name)
        elif action.strip().lower() == "deny":
            deny_access_request(request_id, review_note)
        else:
            raise ValueError("Choose approve or deny.")
        return _redirect("/access-requests")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "access_requests.html",
            _template_context(
                request,
                access_requests=list_access_requests("pending"),
                status="pending",
                error=str(exc),
            ),
            status_code=400,
        )


@app.get("/supervisor/setup", response_class=HTMLResponse)
def supervisor_setup_page(request: Request):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] != "researcher":
        return _redirect("/dashboard")
    try:
        status = get_supervisor_status()
        admins = list_available_admins()
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "supervisor_setup.html",
            _template_context(request, supervisor_status={"state": "error"}, admins=[], error=str(exc)),
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "supervisor_setup.html",
        _template_context(request, supervisor_status=status, admins=admins, error=""),
    )


@app.post("/supervisor/setup", response_class=HTMLResponse)
def supervisor_setup_submit(
    request: Request,
    admin_user_id: str = Form(...),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] != "researcher":
        return _redirect("/dashboard")
    try:
        submit_supervisor_request(admin_user_id)
        return _redirect("/supervisor/setup")
    except Exception as exc:
        try:
            status = get_supervisor_status()
            admins = list_available_admins()
        except Exception:
            status = {"state": "error"}
            admins = []
        return templates.TemplateResponse(
            request,
            "supervisor_setup.html",
            _template_context(request, supervisor_status=status, admins=admins, error=str(exc)),
            status_code=400,
        )


@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request, status: str = "pending"):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] not in {"admin", "main_admin"}:
        return _redirect("/dashboard")
    clean_status = status.strip().lower()
    if clean_status not in {"pending", "approved", "denied", "canceled", "all"}:
        clean_status = "pending"
    requests = list_supervisor_requests("" if clean_status == "all" else clean_status)
    return templates.TemplateResponse(
        request,
        "team.html",
        _template_context(
            request,
            supervisor_requests=requests,
            status=clean_status,
            record_owner_options=_record_owner_options(user),
            error="",
        ),
    )


@app.post("/team/supervisor-requests/review", response_class=HTMLResponse)
def team_supervisor_request_review(
    request: Request,
    request_id: str = Form(...),
    action: str = Form(...),
    review_note: str = Form(""),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if user["role"] not in {"admin", "main_admin"}:
        return _redirect("/dashboard")
    try:
        review_supervisor_request(request_id, action, review_note)
        return _redirect("/team")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "team.html",
            _template_context(
                request,
                supervisor_requests=list_supervisor_requests("pending"),
                status="pending",
                record_owner_options=_record_owner_options(user),
                error=str(exc),
            ),
            status_code=400,
        )


@app.post("/record-view")
async def update_record_view(request: Request):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    form = await request.form()
    allowed = [item["username"] for item in _record_owner_options(user)]
    requested = [str(item).strip() for item in form.getlist("record_owners")]
    if str(form.get("include_all", "")).strip() == "1":
        selected = allowed
    else:
        selected = [item for item in requested if item in allowed]
    request.session["record_owner_filter"] = selected or [user["username"]]
    destination = str(form.get("next", "/dashboard")).strip() or "/dashboard"
    if not destination.startswith("/"):
        destination = "/dashboard"
    return _redirect(destination)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    selected_record_owners = _selected_record_owners(request, user)
    runs = _sort_runs(_load_runs_for_owners(selected_record_owners))
    metrics = _records_metrics(runs)
    inventory_count = len(_inventory_rows(user["username"], None))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _template_context(
            request,
            draft_count=metrics["drafts"],
            record_count=metrics["submitted"],
            inventory_count=inventory_count,
            record_owner_options=_record_owner_options(user),
            selected_record_owners=selected_record_owners,
            record_view_all=len(selected_record_owners) == len(_record_owner_options(user)),
        ),
    )


@app.get("/experiments/search", response_class=HTMLResponse)
def experiment_search(request: Request, q: str = ""):
    try:
        _require_user(request)
    except PermissionError:
        return _redirect("/")
    query = q.strip().lower()
    matches = [
        exp for exp in EXPERIMENT_TYPES
        if not query or query in exp.lower()
    ]
    return templates.TemplateResponse(
        request,
        "experiment_search.html",
        _template_context(request, query=q, matches=matches),
    )


@app.get("/experiments/new", response_class=HTMLResponse)
def experiment_new(request: Request, experiment_type: str = ""):
    try:
        _require_user(request)
    except PermissionError:
        return _redirect("/")
    label, code = _normalize_experiment_type(experiment_type or "Immunohistochemistry (IHC)")
    if code == "IHC":
        return _redirect("/ihc")
    if code == "PCR":
        return _redirect("/pcr")
    return _redirect("/experiments/search")


@app.post("/experiments/new", response_class=HTMLResponse)
def experiment_create(
    request: Request,
    experiment_type: str = Form(...),
    run_name: str = Form(...),
    notes: str = Form(""),
    payload_json: str = Form("{}"),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")

    return _redirect("/experiments/search")


@app.get("/experiments", response_class=HTMLResponse)
def experiments(request: Request, saved: str = "", file: str = ""):
    return _redirect("/records")


@app.get("/ihc", response_class=HTMLResponse)
def ihc_workflow(request: Request, run_id: str = "", modify: int = 0):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    accessible_run = _find_accessible_run(request, user, run_id) if run_id.strip() else None
    existing_run = accessible_run[0] if accessible_run else None
    record_owner = accessible_run[1] if accessible_run else user["username"]
    initial_payload = existing_run["payload"] if existing_run else {}
    is_draft = str(initial_payload.get("record_status", "Draft" if not existing_run else "Submitted")).lower() == "draft"
    editable = not existing_run or is_draft or bool(modify)
    storage_suggestions = get_user_storage_locations(record_owner)
    return templates.TemplateResponse(
        request,
        "ihc_workflow.html",
        _template_context(
            request,
            primaries_json=json.dumps(_ihc_primaries(user["username"])),
            secondaries_json=json.dumps(_ihc_secondaries(user["username"])),
            storage_suggestions_json=json.dumps(storage_suggestions),
            initial_payload_json=json.dumps(initial_payload),
            initial_run_id=existing_run["run_id"] if existing_run else "",
            initial_editable=editable,
            initial_status=str(initial_payload.get("record_status", "Draft" if not existing_run else "Submitted")),
        ),
    )


@app.post("/ihc/save")
def ihc_save(
    request: Request,
    payload_json: str = Form(...),
    run_id: str = Form(""),
    save_mode: str = Form("submitted"),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")

    payload = _parse_payload_json(payload_json)
    accessible_run = _find_accessible_run(request, user, run_id) if run_id.strip() else None
    existing_run = accessible_run[0] if accessible_run else None
    record_owner = accessible_run[1] if accessible_run else user["username"]
    payload["username"] = record_owner
    payload["researcher_user_id"] = _researcher_user_id(record_owner)
    payload["researcher_name"] = _display_researcher_name(record_owner)
    payload["stage"] = "final_slide_book"
    payload["experiment_type"] = "IHC"
    payload["started_record_on"] = str(payload.get("started_record_on") or (existing_run["payload"].get("started_record_on") if existing_run else "") or _now_iso())
    payload["last_closed"] = _now_iso()
    payload["record_status"] = "Draft" if save_mode.strip().lower() == "draft" else "Submitted"

    slides = payload.get("slides", [])
    if not isinstance(slides, list):
        slides = []
        payload["slides"] = slides

    if existing_run:
        out_file = update_experiment_payload(existing_run["run_id"], payload)
        saved_run_id = existing_run["run_id"]
    else:
        saved_run_id, out_file = save_experiment_payload(record_owner, payload)

    for slide in slides:
        if not isinstance(slide, dict):
            continue
        storage = str(slide.get("storage_location", "")).strip()
        if storage:
            try:
                remember_user_storage_location(record_owner, storage)
            except Exception:
                pass
        if payload["record_status"] != "Submitted":
            continue
        slide_id = str(slide.get("slide_id", "")).strip()
        if not slide_id:
            continue
        disposition = str(slide.get("disposition", "")).strip().lower()
        if disposition not in SLIDE_DISPOSITIONS:
            disposition = "stored"
        imaging = str(slide.get("imaging", "")).strip() or "pending"
        upsert_standard_object(
            record_owner,
            "slibrary",
            slide_id,
            {
                "group_name": str(slide.get("group_name", "")).strip(),
                "primary_set": str(slide.get("primary_set", "")).strip(),
                "primary_mm": str(slide.get("primary_mm", "")).strip(),
                "secondary_set": str(slide.get("secondary_set", "")).strip(),
                "secondary_mm": str(slide.get("secondary_mm", "")).strip(),
                "researcher_user_id": _researcher_user_id(record_owner),
                "researcher_name": _display_researcher_name(record_owner),
                "edu_enabled": "True" if payload.get("edu_enabled") else "False",
                "disposition": disposition,
                "imaging": imaging,
                "storage_location": storage,
                "planned_use": str(slide.get("planned_use", "")).strip(),
                "visibility": "Personal",
            },
        )

    return _redirect(f"/records?saved={saved_run_id[:8]}&file={out_file.name}")


@app.get("/pcr", response_class=HTMLResponse)
def pcr_workflow(request: Request, run_id: str = "", modify: int = 0):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    accessible_run = _find_accessible_run(request, user, run_id) if run_id.strip() else None
    existing_run = accessible_run[0] if accessible_run else None
    record_owner = accessible_run[1] if accessible_run else user["username"]
    initial_payload = existing_run["payload"] if existing_run else {}
    is_draft = str(initial_payload.get("record_status", "Draft" if not existing_run else "Submitted")).lower() == "draft"
    editable = not existing_run or is_draft or bool(modify)
    storage_suggestions = get_user_storage_locations(record_owner)
    return templates.TemplateResponse(
        request,
        "pcr_workflow.html",
        _template_context(
            request,
            initial_payload_json=json.dumps(initial_payload),
            pcr_primers_json=json.dumps(_pcr_primers(user["username"])),
            storage_suggestions_json=json.dumps(storage_suggestions),
            initial_run_id=existing_run["run_id"] if existing_run else "",
            initial_editable=editable,
            initial_status=str(initial_payload.get("record_status", "Draft" if not existing_run else "Submitted")),
        ),
    )


@app.post("/pcr/save")
def pcr_save(
    request: Request,
    payload_json: str = Form(...),
    run_id: str = Form(""),
    save_mode: str = Form("submitted"),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")

    payload = _parse_payload_json(payload_json)
    accessible_run = _find_accessible_run(request, user, run_id) if run_id.strip() else None
    existing_run = accessible_run[0] if accessible_run else None
    record_owner = accessible_run[1] if accessible_run else user["username"]
    payload["username"] = record_owner
    payload["researcher_user_id"] = _researcher_user_id(record_owner)
    payload["researcher_name"] = _display_researcher_name(record_owner)
    payload["experiment_type"] = "PCR"
    payload["started_record_on"] = str(payload.get("started_record_on") or (existing_run["payload"].get("started_record_on") if existing_run else "") or _now_iso())
    payload["last_closed"] = _now_iso()
    payload["record_status"] = "Draft" if save_mode.strip().lower() == "draft" else "Submitted"
    tubes = payload.get("tubes", [])
    if not isinstance(tubes, list):
        tubes = []
        payload["tubes"] = tubes
    for tube in tubes:
        if not isinstance(tube, dict):
            continue
        storage = str(tube.get("storage_location", "")).strip()
        if storage:
            try:
                remember_user_storage_location(record_owner, storage)
            except Exception:
                pass
    if existing_run:
        out_file = update_experiment_payload(existing_run["run_id"], payload)
        saved_run_id = existing_run["run_id"]
    else:
        saved_run_id, out_file = save_experiment_payload(record_owner, payload)
    _sync_generated_pcr_products(record_owner, saved_run_id, payload)
    return _redirect(f"/records?saved={saved_run_id[:8]}&file={out_file.name}")


def _normalize_inventory_visibility_filter(raw: str) -> str:
    normalized = raw.strip().lower()
    return normalized if normalized in {"all", "shared", "personal"} else "all"


@app.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request, category: str = "", imported: str = "", deleted: str = "", visibility: str = "all"):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    visibility_filter = _normalize_inventory_visibility_filter(visibility)
    notice = ""
    if imported.isdigit():
        notice = f"Imported {imported} row{'s' if imported != '1' else ''} into inventory."
    elif deleted.isdigit():
        notice = f"Backed up and deleted {deleted} row{'s' if deleted != '1' else ''} from inventory."
    return templates.TemplateResponse(
        request,
        "inventory.html",
        _template_context(
            request,
            inventory_sections=_inventory_page_sections(user["username"], user["role"], visibility_filter),
            inventory_role=user["role"],
            selected_category=category.strip(),
            inventory_visibility=visibility_filter,
            notice=notice,
        ),
    )


@app.post("/inventory", response_class=HTMLResponse)
async def inventory_upsert(request: Request):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    form = await request.form()
    selected = str(form.get("category", "")).strip()
    original_name = str(form.get("original_name", "")).strip()
    fields_json = str(form.get("fields_json", "{}")).strip() or "{}"
    try:
        field_values = _parse_payload_json(fields_json)
        clean_name = str(field_values.get("name", "")).strip()
        if not clean_name:
            raise ValueError("Name is required.")
        metadata = _coerce_inventory_metadata(user, selected, clean_name, field_values, original_name=original_name)

        upsert_standard_object(user["username"], selected, clean_name, metadata)
        if _is_inventory_admin(user["role"]) and original_name and original_name != clean_name:
            delete_standard_object(user["username"], selected, original_name)
        return _redirect(f"/inventory?category={selected}")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "inventory.html",
            _template_context(
                request,
                inventory_sections=[
                    {
                        "key": section_key,
                        "label": section_label,
                        "categories": [_inventory_table_rows(user["username"], user["role"], item_category) for item_category in section_categories],
                    }
                    for section_key, section_label, section_categories in INVENTORY_SECTIONS
                ],
                inventory_role=user["role"],
                selected_category=selected,
                inventory_visibility="all",
                error=str(exc),
            ),
            status_code=400,
        )


@app.post("/inventory/import")
async def inventory_import_csv(
    request: Request,
    category: str = Form(...),
    visibility: str = Form(""),
    csv_file: UploadFile = File(...),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    selected = category.strip()
    if selected not in INVENTORY_CATEGORY_LABELS:
        return _redirect("/inventory")
    try:
        raw_bytes = await csv_file.read()
        text = raw_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV must include a header row.")
        aliases = _inventory_field_aliases(selected)
        imported_count = 0
        skipped_rows: list[str] = []
        for index, row in enumerate(reader, start=2):
            normalized_values: dict[str, str] = {}
            for header, value in row.items():
                if header is None:
                    continue
                field = aliases.get(_normalize_inventory_field_key(header))
                if field is None:
                    continue
                normalized_values[field] = str(value or "").strip()
            clean_name = str(normalized_values.get("name", "")).strip()
            if not clean_name:
                skipped_rows.append(str(index))
                continue
            metadata = _coerce_inventory_metadata(
                user,
                selected,
                clean_name,
                normalized_values,
                default_visibility=visibility,
            )
            upsert_standard_object(user["username"], selected, clean_name, metadata)
            imported_count += 1
        if skipped_rows and imported_count == 0:
            raise ValueError("No rows were imported. Make sure the CSV has a Name column.")
        return _redirect(f"/inventory?category={selected}&imported={imported_count}")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "inventory.html",
            _template_context(
                request,
                inventory_sections=_inventory_page_sections(user["username"], user["role"]),
                inventory_role=user["role"],
                selected_category=selected,
                inventory_visibility="all",
                error=str(exc),
            ),
            status_code=400,
        )


@app.post("/inventory/columns")
async def inventory_add_column(request: Request):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if not _is_inventory_admin(user["role"]):
        return _redirect("/inventory")
    form = await request.form()
    category = str(form.get("category", "")).strip()
    label = str(form.get("label", "")).strip()
    requested_key = str(form.get("field_key", "")).strip()
    if not category or not label:
        return _redirect("/inventory")
    field_key = _normalize_inventory_field_key(requested_key or label)
    if not field_key:
        return _redirect("/inventory")
    schema = _load_inventory_schema()
    existing = {item["field"] for item in _inventory_fields(category)}
    if field_key not in existing:
        schema.setdefault(category, []).append({"field": field_key, "label": label})
        _save_inventory_schema(schema)
    return _redirect("/inventory")


@app.post("/inventory/delete")
def inventory_delete(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    if not _is_inventory_admin(user["role"]):
        return _redirect("/inventory")
    delete_standard_object(user["username"], category.strip(), name.strip())
    return _redirect(f"/inventory?category={category.strip()}")


@app.post("/inventory/delete-all")
def inventory_delete_all(
    request: Request,
    category: str = Form(...),
    visibility: str = Form("all"),
    confirm_text: str = Form(""),
):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    selected = category.strip()
    visibility_filter = _normalize_inventory_visibility_filter(visibility)
    if user["role"] != "main_admin" or selected not in INVENTORY_CATEGORY_LABELS:
        return _redirect("/inventory")
    try:
        if confirm_text.strip().upper() != "DELETE":
            raise ValueError("Type DELETE to confirm deleting all rows in this sheet.")
        rows = _filter_inventory_rows_for_visibility(_inventory_rows(user["username"], selected), visibility_filter)
        if not rows:
            return _redirect(f"/inventory?category={selected}&visibility={visibility_filter}&deleted=0")
        backup_inventory_rows(selected, rows, visibility_filter)
        deleted_count = delete_standard_objects_by_ids([int(row.get("id", 0) or 0) for row in rows])
        return _redirect(f"/inventory?category={selected}&visibility={visibility_filter}&deleted={deleted_count}")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "inventory.html",
            _template_context(
                request,
                inventory_sections=_inventory_page_sections(user["username"], user["role"], visibility_filter),
                inventory_role=user["role"],
                selected_category=selected,
                inventory_visibility=visibility_filter,
                error=str(exc),
            ),
            status_code=400,
        )


@app.get("/records", response_class=HTMLResponse)
def records(request: Request, scope: str = "saved", saved: str = "", file: str = ""):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    selected_record_owners = _selected_record_owners(request, user)
    runs = _sort_runs(_load_runs_for_owners(selected_record_owners))
    filtered = _filtered_records(runs, scope)
    return templates.TemplateResponse(
        request,
        "orphanage.html",
        _template_context(
            request,
            runs=filtered,
            scope=scope,
            saved=saved,
            file=file,
            record_owner_options=_record_owner_options(user),
            selected_record_owners=selected_record_owners,
        ),
    )


@app.get("/records/open/{run_id}")
def open_record(request: Request, run_id: str):
    try:
        user = _require_user(request)
    except PermissionError:
        return _redirect("/")
    accessible_run = _find_accessible_run(request, user, run_id)
    if accessible_run is None:
        return _redirect("/records")
    run = accessible_run[0]
    payload = run["payload"]
    experiment_type = str(payload.get("experiment_type", run.get("experiment_type", ""))).strip().upper()
    if experiment_type == "IHC":
        return _redirect(f"/ihc?run_id={run_id}")
    if experiment_type == "PCR":
        return _redirect(f"/pcr?run_id={run_id}")
    return _redirect("/records")


@app.get("/orphanage", response_class=HTMLResponse)
def orphanage(request: Request):
    return _redirect("/records")


@app.get("/protocol-builder", response_class=HTMLResponse)
def protocol_builder(request: Request):
    return _redirect("/experiments/search")


@app.post("/protocol-builder", response_class=HTMLResponse)
def protocol_builder_generate(
    request: Request,
    experiment_type: str = Form(...),
    title: str = Form(""),
    details: str = Form(""),
):
    return _redirect("/experiments/search")
