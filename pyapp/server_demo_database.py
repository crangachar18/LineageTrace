from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from beta_minimal.server_demo_client import (
    DemoServerError,
    create_record,
    delete_standard_object as delete_standard_object_remote,
    list_records,
    list_standard_objects as list_standard_objects_remote,
    upsert_standard_object as upsert_standard_object_remote,
)
from pyapp.app_config import get_shared_inventory_owner
from pyapp.database import EXPORTS_DIR


def save_experiment_payload(username: str, payload: dict[str, Any]) -> tuple[str, Path]:
    experiment_type = str(payload.get("experiment_type", "")).strip().upper()
    if not experiment_type:
        stage = str(payload.get("stage", "")).strip().lower()
        experiment_type = "IHC" if stage == "final_slide_book" else "PCR"
    if experiment_type not in {"IHC", "PCR"}:
        raise RuntimeError(f"Server demo mode only supports IHC and PCR right now, not {experiment_type or 'unknown'}")
    response = create_record(experiment_type, payload)
    run_id = str(response.get("run_id", "")).strip()
    if not run_id:
        raise RuntimeError("Server did not return a run id.")
    created_at = datetime.now(timezone.utc).isoformat()
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = EXPORTS_DIR / f"{created_at[:10]}_{username}_{run_id[:8]}.json"
    out_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return run_id, out_file


def update_experiment_payload(run_id: str, payload: dict[str, Any]) -> Path:
    raise RuntimeError("Editing existing records through the demo server is not implemented yet.")


def list_experiment_runs(username: str) -> list[dict[str, str]]:
    response = list_records(scope="mine")
    items = response.get("items")
    rows = items if isinstance(items, list) else []
    out: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        payload_json = json.dumps(payload if isinstance(payload, dict) else {}, indent=2, sort_keys=True)
        out.append(
            {
                "run_id": str(item.get("run_id", "")).strip(),
                "created_at": str(item.get("created_at", "")).strip(),
                "payload_json": payload_json,
            }
        )
    return out


def list_standard_objects(username: str, category: str | None = None) -> list[dict[str, Any]]:
    effective_username = _effective_username(username, category or "")
    response = list_standard_objects_remote(effective_username, category or "")
    items = response.get("items")
    rows = items if isinstance(items, list) else []
    out: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            out.append(item)
    return out


def upsert_standard_object(username: str, category: str, name: str, metadata: dict[str, Any] | None = None) -> None:
    effective_username = _effective_username(username, category)
    upsert_standard_object_remote(effective_username, category, name, metadata or {})


def delete_standard_object(username: str, category: str, name: str) -> None:
    effective_username = _effective_username(username, category)
    delete_standard_object_remote(effective_username, category, name)


def _effective_username(username: str, category: str) -> str:
    if category in {"primary_antibody", "secondary_antibody", "slibrary", "pcr_amplicon"}:
        shared_owner = get_shared_inventory_owner().strip()
        if shared_owner:
            return shared_owner
    return username
