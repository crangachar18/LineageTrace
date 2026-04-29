from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _strip_env_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        return cleaned[1:-1]
    return cleaned


def _load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value)


def load_project_environment() -> None:
    """Load local connection settings without overriding the shell environment."""
    root = _project_root()
    _load_env_file(root / ".env")
    _load_env_file(root / "lineagetrace_connections.env")
    explicit_config = os.environ.get("LINEAGETRACE_CONFIG_FILE", "").strip()
    if explicit_config:
        _load_env_file(Path(explicit_config).expanduser())


load_project_environment()


@dataclass(slots=True)
class ConnectionSettings:
    mode: str = "local"
    server_url: str = ""
    server_label: str = ""
    anon_key: str = ""


@dataclass(slots=True)
class SessionSettings:
    connection_mode: str = "local"
    server_url: str = ""
    username: str = ""
    display_name: str = ""
    role: str = ""
    access_token: str = ""
    refresh_token: str = ""


def _default_data_dir() -> Path:
    env_dir = os.environ.get("SLIDEAPP_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()

    if os.name == "posix" and "darwin" in os.uname().sysname.lower():
        return Path.home() / "Library" / "Application Support" / "SlideApp"

    return Path.home() / ".local" / "share" / "SlideApp"


def connection_settings_path() -> Path:
    return _default_data_dir() / "connection_settings.json"


def session_settings_path() -> Path:
    return _default_data_dir() / "session_settings.json"


def desktop_auth_redirect_url() -> str:
    return "lineagetrace://auth-callback"


def load_connection_settings() -> ConnectionSettings | None:
    env_mode = os.environ.get("SLIDEAPP_CONNECTION_MODE", "").strip().lower()
    if env_mode == "supabase":
        server_url = (
            os.environ.get("SLIDEAPP_SUPABASE_URL", "").strip()
            or os.environ.get("SUPABASE_URL", "").strip()
        )
        anon_key = (
            os.environ.get("SLIDEAPP_SUPABASE_ANON_KEY", "").strip()
            or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        )
        if not server_url or not anon_key:
            return None
        return ConnectionSettings(
            mode="supabase",
            server_url=server_url,
            server_label=os.environ.get("SLIDEAPP_SUPABASE_LABEL", "Supabase").strip(),
            anon_key=anon_key,
        )

    path = connection_settings_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode", "local")).strip().lower() or "local"
    if mode == "lab_server":
        mode = "server_demo"
    return ConnectionSettings(
        mode=mode,
        server_url=str(raw.get("server_url", "")).strip(),
        server_label=str(raw.get("server_label", "")).strip(),
        anon_key=str(raw.get("anon_key", "")).strip(),
    )


def save_connection_settings(settings: ConnectionSettings) -> None:
    path = connection_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def clear_connection_settings() -> None:
    path = connection_settings_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def load_session_settings() -> SessionSettings | None:
    path = session_settings_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return SessionSettings(
        connection_mode=str(raw.get("connection_mode", "local")).strip().lower() or "local",
        server_url=str(raw.get("server_url", "")).strip(),
        username=str(raw.get("username", "")).strip(),
        display_name=str(raw.get("display_name", "")).strip(),
        role=str(raw.get("role", "")).strip(),
        access_token=str(raw.get("access_token", "")).strip(),
        refresh_token=str(raw.get("refresh_token", "")).strip(),
    )


def save_session_settings(settings: SessionSettings) -> None:
    path = session_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def clear_session_settings() -> None:
    path = session_settings_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def apply_connection_settings(settings: ConnectionSettings | None) -> None:
    if settings is None:
        return
    os.environ["SLIDEAPP_CONNECTION_MODE"] = settings.mode
    if settings.mode == "local":
        os.environ["SLIDEAPP_BACKEND"] = "local"
    if settings.server_url:
        os.environ["SLIDEAPP_SERVER_URL"] = settings.server_url
    else:
        os.environ.pop("SLIDEAPP_SERVER_URL", None)


def is_valid_server_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def describe_connection_settings(settings: ConnectionSettings | None) -> str:
    if settings is None:
        return "Connection not configured"
    if settings.mode == "server_demo":
        label = settings.server_label or settings.server_url or "Lab server"
        return f"Connected target: {label}"
    if settings.mode == "supabase":
        label = settings.server_label or settings.server_url or "Supabase project"
        return f"Connected target: {label}"
    return "Connected target: Local only"


def is_ihc_only_mode() -> bool:
    return os.environ.get("SLIDEAPP_APP_MODE", "").strip().lower() == "ihc_only"


def is_login_bypass_enabled() -> bool:
    return os.environ.get("SLIDEAPP_LOGIN_BYPASS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_shared_inventory_owner() -> str:
    return os.environ.get("SLIDEAPP_SHARED_INVENTORY_OWNER", "").strip()


def get_default_username() -> str:
    return os.environ.get("SLIDEAPP_DEFAULT_USERNAME", "").strip() or "lab_user"


def get_allowed_experiment_tokens() -> set[str]:
    raw = os.environ.get("SLIDEAPP_ALLOWED_EXPERIMENTS", "").strip()
    if not raw:
        return set()
    return {
        token.strip().lower()
        for token in raw.split(",")
        if token.strip()
    }


def get_authorized_login_emails() -> set[str]:
    raw = (
        os.environ.get("LINEAGETRACE_AUTHORIZED_EMAILS", "").strip()
        or os.environ.get("SLIDEAPP_AUTHORIZED_EMAILS", "").strip()
    )
    if not raw:
        return set()
    return {
        email.strip().lower()
        for email in raw.replace("\n", ",").split(",")
        if email.strip()
    }


def get_display_name_overrides() -> dict[str, str]:
    raw = os.environ.get("LINEAGETRACE_DISPLAY_NAME_OVERRIDES", "").strip()
    if not raw:
        return {}
    overrides: dict[str, str] = {}
    for item in raw.replace("\n", ",").split(","):
        entry = item.strip()
        if not entry:
            continue
        if "=" in entry:
            identifier, display_name = entry.split("=", 1)
        elif ":" in entry:
            identifier, display_name = entry.split(":", 1)
        else:
            continue
        clean_identifier = identifier.strip().lower()
        clean_display_name = display_name.strip()
        if clean_identifier and clean_display_name:
            overrides[clean_identifier] = clean_display_name
    return overrides
