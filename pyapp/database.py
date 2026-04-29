from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sys

from pyapp.app_config import load_project_environment

load_project_environment()

# ---------------------------------------------------------------------------
# Backend routing
# ---------------------------------------------------------------------------
_BACKEND = os.environ.get("SLIDEAPP_BACKEND", "local").strip().lower()
_USE_CLOUD = _BACKEND == "cloud"
_USE_SHEETS = _BACKEND == "sheets"
if _USE_CLOUD:
    from pyapp import cloud_database as _cloud  # noqa: E402
if _USE_SHEETS:
    from pyapp import sheets_database as _sheets  # noqa: E402


def _use_supabase_client() -> bool:
    return os.environ.get("SLIDEAPP_CONNECTION_MODE", "").strip().lower() == "supabase"


def _use_server_demo_client() -> bool:
    return os.environ.get("SLIDEAPP_CONNECTION_MODE", "").strip().lower() == "server_demo"

APP_NAME = "SlideApp"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PBKDF2_ROUNDS = 120_000


def _default_data_dir() -> Path:
    env_dir = os.environ.get("SLIDEAPP_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()

    # macOS default for distributable app data.
    if os.name == "posix" and "darwin" in os.uname().sysname.lower():
        return Path.home() / "Library" / "Application Support" / APP_NAME

    # Generic fallback for non-mac platforms.
    return Path.home() / ".local" / "share" / APP_NAME


DATA_DIR = _default_data_dir()
DB_PATH = DATA_DIR / "slideapp.db"
EXPORTS_DIR = DATA_DIR / "exports"

LEGACY_DB_PATH = SOURCE_ROOT / "db" / "slideapp.db"
LEGACY_EXPORTS_DIR = SOURCE_ROOT / "exports"


def _migrate_legacy_data_if_needed() -> None:
    if DB_PATH.exists():
        return

    if LEGACY_DB_PATH.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)

    if LEGACY_EXPORTS_DIR.exists():
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        for src in LEGACY_EXPORTS_DIR.glob("*.json"):
            dst = EXPORTS_DIR / src.name
            if not dst.exists():
                shutil.copy2(src, dst)


def _likely_external_exports_dir() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    # .../<project>/dist/SlideAppBeta.app/Contents/MacOS/SlideAppBeta
    # Want .../<project>/exports
    try:
        project_root = exe.parent.parent.parent.parent.parent
    except IndexError:
        return None
    candidate = project_root / "exports"
    return candidate if candidate.exists() else None


def _import_export_jsons_into_runs_if_empty(conn: sqlite3.Connection) -> None:
    run_count = conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0]
    if int(run_count) > 0:
        return

    export_dirs: list[Path] = [EXPORTS_DIR, LEGACY_EXPORTS_DIR]
    external = _likely_external_exports_dir()
    if external is not None:
        export_dirs.append(external)

    seen_content: set[str] = set()
    for export_dir in export_dirs:
        if not export_dir.exists():
            continue
        for src in sorted(export_dir.glob("*.json")):
            try:
                payload_text = src.read_text(encoding="utf-8")
                payload = json.loads(payload_text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            username = str(payload.get("username", "")).strip() or "unknown_user"
            if username == "unknown_user":
                continue

            # Ensure FK user exists for imported historical exports.
            user_row = conn.execute(
                "SELECT username FROM app_users WHERE username = ?",
                (username,),
            ).fetchone()
            if user_row is None:
                salt_hex = secrets.token_hex(16)
                password_hash = _hash_password("temporary", salt_hex)
                conn.execute(
                    """
                    INSERT INTO app_users (username, role, password_salt, password_hash)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, "researcher", salt_hex, password_hash),
                )

            # Prevent duplicate inserts when scanning multiple directories.
            content_key = payload_text.strip()
            if content_key in seen_content:
                continue
            seen_content.add(content_key)

            created_at = datetime.now(timezone.utc).isoformat()
            stem = src.stem
            parts = stem.split("_")
            run_id = parts[-1] if parts else uuid.uuid4().hex[:8]
            if not run_id:
                run_id = uuid.uuid4().hex[:8]
            run_id = run_id.replace("-", "")
            if len(run_id) < 8:
                run_id = f"{run_id}{uuid.uuid4().hex[:8-len(run_id)]}"

            # Ensure unique key.
            exists = conn.execute(
                "SELECT run_id FROM experiment_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if exists is not None:
                run_id = uuid.uuid4().hex

            conn.execute(
                """
                INSERT INTO experiment_runs (run_id, username, created_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, username, created_at, payload_text),
            )


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return digest.hex()


def _migrate_standards_schema_if_needed(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'standards_objects'
        """
    ).fetchone()
    if row is None:
        return
    create_sql = str(row[0] or "")
    if (
        "slibrary" in create_sql
        and
        "primary_antibody" in create_sql
        and "secondary_antibody" in create_sql
        and "mini_prep" in create_sql
        and "pcr_amplicon" in create_sql
        and "pcr_primer" in create_sql
        and "block" in create_sql
        and "stock" in create_sql
        and "plate" in create_sql
        and "restriction_enzyme" in create_sql
    ):
        return

    conn.execute(
        """
        CREATE TABLE standards_objects_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL,
          category TEXT NOT NULL CHECK (
            category IN (
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
          name TEXT NOT NULL,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (username, category, name),
          FOREIGN KEY (username) REFERENCES app_users(username) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO standards_objects_new (id, username, category, name, metadata_json, created_at, updated_at)
        SELECT
          id,
          username,
          CASE
            WHEN category = 'antibody' THEN 'primary_antibody'
            ELSE category
          END AS category,
          name,
          metadata_json,
          created_at,
          updated_at
        FROM standards_objects
        """
    )
    conn.execute("DROP TABLE standards_objects")
    conn.execute("ALTER TABLE standards_objects_new RENAME TO standards_objects")


def init_auth_db() -> None:
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.init_auth_db()
    if _USE_CLOUD:
        return _cloud.init_auth_db()
    if _USE_SHEETS:
        _sheets.init_auth_db()
    _migrate_legacy_data_if_needed()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
              username TEXT PRIMARY KEY,
              role TEXT NOT NULL CHECK (role IN ('admin', 'researcher')),
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experiment_runs (
              run_id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              created_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY (username) REFERENCES app_users(username) ON DELETE RESTRICT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_storage_locations (
              username TEXT NOT NULL,
              location TEXT NOT NULL,
              last_used_at TEXT NOT NULL,
              PRIMARY KEY (username, location),
              FOREIGN KEY (username) REFERENCES app_users(username) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standards_objects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL,
              category TEXT NOT NULL CHECK (
                category IN (
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
              name TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE (username, category, name),
              FOREIGN KEY (username) REFERENCES app_users(username) ON DELETE CASCADE
            )
            """
        )
        _migrate_standards_schema_if_needed(conn)

        row = conn.execute(
            "SELECT username FROM app_users WHERE username = ?", ("admin",)
        ).fetchone()

        if row is None:
            salt_hex = secrets.token_hex(16)
            password_hash = _hash_password("trial", salt_hex)
            conn.execute(
                """
                INSERT INTO app_users (username, role, password_salt, password_hash)
                VALUES (?, ?, ?, ?)
                """,
                ("admin", "admin", salt_hex, password_hash),
            )

        _import_export_jsons_into_runs_if_empty(conn)
        conn.commit()


def verify_credentials(username: str, password: str) -> str | None:
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.verify_credentials(username, password)
    if _USE_CLOUD:
        return _cloud.verify_credentials(username, password)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT role, password_salt, password_hash
            FROM app_users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

    if row is None:
        return None

    role, salt_hex, expected_hash = row
    provided_hash = _hash_password(password, salt_hex)
    if secrets.compare_digest(provided_hash, expected_hash):
        return role

    return None


def _ensure_local_user_exists(username: str, role: str = "researcher") -> None:
    clean_username = username.strip()
    if not clean_username:
        return
    with sqlite3.connect(DB_PATH) as conn:
        exists = conn.execute(
            "SELECT username FROM app_users WHERE username = ?",
            (clean_username,),
        ).fetchone()
        if exists is not None:
            return
        salt_hex = secrets.token_hex(16)
        password_hash = _hash_password("temporary", salt_hex)
        conn.execute(
            """
            INSERT INTO app_users (username, role, password_salt, password_hash)
            VALUES (?, ?, ?, ?)
            """,
            (clean_username, role, salt_hex, password_hash),
        )
        conn.commit()


def get_user_storage_locations(username: str) -> list[str]:
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.get_user_storage_locations(username)
    if _USE_CLOUD:
        return _cloud.get_user_storage_locations(username)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT location
            FROM user_storage_locations
            WHERE username = ?
            ORDER BY last_used_at DESC
            """,
            (username,),
        ).fetchall()
    return [row[0] for row in rows]


def remember_user_storage_location(username: str, location: str) -> None:
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.remember_user_storage_location(username, location)
    if _USE_CLOUD:
        return _cloud.remember_user_storage_location(username, location)
    loc = location.strip()
    if not loc:
        return
    _ensure_local_user_exists(username)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_storage_locations (username, location, last_used_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username, location) DO UPDATE SET last_used_at = excluded.last_used_at
            """,
            (username, loc, now),
        )
        conn.commit()


def clear_user_storage_locations(username: str | None = None) -> None:
    if _use_server_demo_client():
        raise RuntimeError("Clearing server-demo storage locations is not supported from the local web app helper.")
    if _use_supabase_client():
        raise RuntimeError("Clearing Supabase storage locations is not supported from the local web app helper.")
    if _USE_CLOUD or _USE_SHEETS:
        raise RuntimeError("Clearing remote storage locations is not supported from the local web app helper.")

    with sqlite3.connect(DB_PATH) as conn:
        if username is None:
            conn.execute("DELETE FROM user_storage_locations")
        else:
            conn.execute("DELETE FROM user_storage_locations WHERE username = ?", (username,))
        conn.commit()


def save_experiment_payload(username: str, payload: dict[str, Any]) -> tuple[str, Path]:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.save_experiment_payload(username, payload)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.save_experiment_payload(username, payload)
    if _USE_CLOUD:
        return _cloud.save_experiment_payload(username, payload)
    if _USE_SHEETS:
        return _sheets.save_experiment_payload(username, payload)
    run_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload, indent=2, sort_keys=True)

    out_file = EXPORTS_DIR / f"{created_at[:10]}_{username}_{run_id[:8]}.json"
    out_file.write_text(payload_json, encoding="utf-8")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO experiment_runs (run_id, username, created_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, username, created_at, payload_json),
        )
        conn.commit()

    return run_id, out_file


def update_experiment_payload(run_id: str, payload: dict[str, Any]) -> Path:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.update_experiment_payload(run_id, payload)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.update_experiment_payload(run_id, payload)
    if _USE_CLOUD:
        return _cloud.update_experiment_payload(run_id, payload)
    if _USE_SHEETS:
        return _sheets.update_experiment_payload(run_id, payload)
    created_at = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload, indent=2, sort_keys=True)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT username
            FROM experiment_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        username = str(row[0])

        conn.execute(
            """
            UPDATE experiment_runs
            SET payload_json = ?, created_at = ?
            WHERE run_id = ?
            """,
            (payload_json, created_at, run_id),
        )
        conn.commit()

    out_file = EXPORTS_DIR / f"{created_at[:10]}_{username}_{run_id[:8]}.json"
    out_file.write_text(payload_json, encoding="utf-8")
    return out_file


def clear_experiment_runs(username: str | None = None) -> None:
    if _use_server_demo_client():
        raise RuntimeError("Clearing server-demo records is not supported from the local web app helper.")
    if _use_supabase_client():
        raise RuntimeError("Clearing Supabase records is not supported from the local web app helper.")
    if _USE_CLOUD or _USE_SHEETS:
        raise RuntimeError("Clearing remote records is not supported from the local web app helper.")

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        if username is None:
            conn.execute("DELETE FROM experiment_runs")
        else:
            conn.execute("DELETE FROM experiment_runs WHERE username = ?", (username,))
        conn.commit()

    for src in EXPORTS_DIR.glob("*.json"):
        try:
            src.unlink()
        except Exception:
            pass


def list_experiment_runs(username: str) -> list[dict[str, str]]:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.list_experiment_runs(username)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.list_experiment_runs(username)
    if _USE_CLOUD:
        return _cloud.list_experiment_runs(username)
    if _USE_SHEETS:
        return _sheets.list_experiment_runs(username)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT run_id, created_at, payload_json
            FROM experiment_runs
            WHERE username = ?
            ORDER BY created_at DESC
            """,
            (username,),
        ).fetchall()

    out: list[dict[str, str]] = []
    for run_id, created_at, payload_json in rows:
        out.append(
            {
                "run_id": run_id,
                "created_at": created_at,
                "payload_json": payload_json,
            }
        )
    return out


def list_standard_objects(username: str, category: str | None = None) -> list[dict[str, Any]]:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.list_standard_objects(username, category)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.list_standard_objects(username, category)
    if _USE_CLOUD:
        return _cloud.list_standard_objects(username, category)
    if _USE_SHEETS:
        return _sheets.list_standard_objects(username, category)
    with sqlite3.connect(DB_PATH) as conn:
        if category is None:
            rows = conn.execute(
                """
                SELECT id, username, category, name, metadata_json, created_at, updated_at
                FROM standards_objects
                WHERE username = ?
                ORDER BY category ASC, name COLLATE NOCASE ASC
                """,
                (username,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, username, category, name, metadata_json, created_at, updated_at
                FROM standards_objects
                WHERE username = ? AND category = ?
                ORDER BY name COLLATE NOCASE ASC
                """,
                (username, category),
            ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        meta_raw = str(row[4] or "{}")
        try:
            metadata = json.loads(meta_raw)
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        out.append(
            {
                "id": int(row[0]),
                "username": str(row[1]),
                "category": str(row[2]),
                "name": str(row[3]),
                "metadata": metadata,
                "created_at": str(row[5]),
                "updated_at": str(row[6]),
            }
        )
    return out


def list_local_standard_objects(username: str, category: str | None = None) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        if category is None:
            rows = conn.execute(
                """
                SELECT id, username, category, name, metadata_json, created_at, updated_at
                FROM standards_objects
                WHERE username = ?
                ORDER BY category ASC, name COLLATE NOCASE ASC
                """,
                (username,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, username, category, name, metadata_json, created_at, updated_at
                FROM standards_objects
                WHERE username = ? AND category = ?
                ORDER BY name COLLATE NOCASE ASC
                """,
                (username, category),
            ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        meta_raw = str(row[4] or "{}")
        try:
            metadata = json.loads(meta_raw)
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        out.append(
            {
                "id": int(row[0]),
                "username": str(row[1]),
                "category": str(row[2]),
                "name": str(row[3]),
                "metadata": metadata,
                "created_at": str(row[5]),
                "updated_at": str(row[6]),
            }
        )
    return out


def upsert_standard_object(
    username: str,
    category: str,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.upsert_standard_object(username, category, name, metadata)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.upsert_standard_object(username, category, name, metadata)
    if _USE_CLOUD:
        return _cloud.upsert_standard_object(username, category, name, metadata)
    if _USE_SHEETS:
        return _sheets.upsert_standard_object(username, category, name, metadata)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Standard object name is required.")
    if category not in {
        "slibrary",
        "mix_component",
        "mini_prep",
        "pcr_amplicon",
        "pcr_primer",
        "block",
        "stock",
        "plate",
        "rna_probe",
        "primary_antibody",
        "secondary_antibody",
        "restriction_enzyme",
        "antibody",  # legacy compatibility
    }:
        raise ValueError(f"Unsupported standards category: {category}")
    meta_json = json.dumps(metadata or {}, sort_keys=True)
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO standards_objects (username, category, name, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, category, name)
            DO UPDATE SET metadata_json = excluded.metadata_json, updated_at = excluded.updated_at
            """,
            (username, category, clean_name, meta_json, now, now),
        )
        conn.commit()


def delete_standard_object(username: str, category: str, name: str) -> None:
    if _use_server_demo_client():
        from pyapp import server_demo_database as _server_demo  # noqa: E402

        return _server_demo.delete_standard_object(username, category, name)
    if _use_supabase_client():
        from pyapp import supabase_direct_database as _supabase_direct  # noqa: E402

        return _supabase_direct.delete_standard_object(username, category, name)
    if _USE_CLOUD:
        return _cloud.delete_standard_object(username, category, name)
    if _USE_SHEETS:
        return _sheets.delete_standard_object(username, category, name)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            DELETE FROM standards_objects
            WHERE username = ? AND category = ? AND name = ?
            """,
            (username, category, name.strip()),
        )
        conn.commit()
