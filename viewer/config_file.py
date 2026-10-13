"""The optional JSON camera file named by VIEWER_CONFIG.

One camera per instance. The file is authoritative for the fields it sets;
``HIK_*`` environment variables remain the fallback for fields it omits and the
whole configuration when no file is configured. Loading is strict on purpose:
a missing, unreadable, malformed or invalid file stops startup rather than
silently starting from defaults, and an unknown key (a typo like ``usr``) is an
error instead of a field that quietly falls back to something else.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

VIEWER_CONFIG_ENV = "VIEWER_CONFIG"

DEFAULT_HOST = "192.168.0.156"
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = ""
DEFAULT_HTTP_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_TLS = False
DEFAULT_TLS_VERIFY = False

# Every field the camera object may carry. Unknown keys are rejected outright.
CAMERA_KEYS = frozenset(
    {"host", "http_port", "rtsp_port", "user", "password", "tls", "rtsp_tls", "tls_verify"}
)
# The top level names one camera either as "camera" or as a one-entry "cameras".
TOP_LEVEL_KEYS = frozenset({"camera", "cameras"})

# HIK_* fallbacks (and defaults) for the fields that have an environment form.
# tls/tls_verify have none: they are file-only, so an env-only setup is
# byte-identical to before.
_STR_FALLBACKS = {"host": "HIK_HOST", "user": "HIK_USER", "password": "HIK_PASSWORD"}
_INT_FALLBACKS = {"http_port": "HIK_HTTP_PORT", "rtsp_port": "HIK_RTSP_PORT"}
_STR_DEFAULTS = {"host": DEFAULT_HOST, "user": DEFAULT_USER, "password": DEFAULT_PASSWORD}
_INT_DEFAULTS = {"http_port": DEFAULT_HTTP_PORT, "rtsp_port": DEFAULT_RTSP_PORT}
_SECRET_FIELDS = frozenset({"password"})


class ConfigFileError(Exception):
    """VIEWER_CONFIG is set but cannot be used; startup must stop."""


class CameraFields(TypedDict, total=False):
    """The validated camera fields exactly as the file set them."""

    host: str
    user: str
    password: str
    http_port: int
    rtsp_port: int
    tls: bool
    rtsp_tls: bool
    tls_verify: bool


@dataclass(frozen=True)
class CameraSettings:
    """The camera's resolved connection settings (file merged over HIK_*)."""

    host: str
    user: str
    password: str
    http_port: int
    rtsp_port: int
    tls: bool
    rtsp_tls: bool
    tls_verify: bool


def _names(keys: set[str]) -> str:
    return ", ".join(f'"{key}"' for key in sorted(keys))


def load(path: str) -> CameraFields:
    """Read, parse and validate the file at *path*; raises ConfigFileError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"cannot read the config file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigFileError(f"the config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigFileError(f"the config file {path} must contain a JSON object")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigFileError(
            f"unknown top-level key {_names(unknown)} in {path} (expected 'camera' or 'cameras')"
        )
    if "camera" in data and "cameras" in data:
        raise ConfigFileError(f"{path}: use either 'camera' or 'cameras', not both")
    if "cameras" in data:
        camera = _single_camera(data["cameras"], path)
    elif "camera" in data:
        camera = data["camera"]
    else:
        raise ConfigFileError(f"{path}: no camera: the file must contain a 'camera' object")
    if not isinstance(camera, dict):
        raise ConfigFileError(f"{path}: 'camera' must be a JSON object")
    return _validate_camera_fields(camera, path)


def _single_camera(cameras: object, path: str) -> object:
    if not isinstance(cameras, list):
        raise ConfigFileError(f"{path}: 'cameras' must be a JSON array")
    if len(cameras) != 1:
        raise ConfigFileError(
            f"{path}: this build supports one camera per instance; 'cameras' has "
            f"{len(cameras)} entries — run one instance per camera"
        )
    return cameras[0]


def _validate_camera_fields(camera: Mapping[str, object], path: str) -> CameraFields:
    unknown = set(camera) - CAMERA_KEYS
    if unknown:
        raise ConfigFileError(
            f"{path}: unknown camera key {_names(unknown)} "
            f"(expected one of: {', '.join(sorted(CAMERA_KEYS))})"
        )
    fields: CameraFields = {}
    if "host" in camera:
        host = camera["host"]
        if not isinstance(host, str) or not host.strip():
            raise ConfigFileError(f"{path}: camera.host must be a non-empty string")
        fields["host"] = host
    if "user" in camera:
        fields["user"] = _require_string(camera["user"], "user", path)
    if "password" in camera:
        fields["password"] = _require_string(camera["password"], "password", path)
    if "http_port" in camera:
        fields["http_port"] = _require_port(camera["http_port"], "http_port", path)
    if "rtsp_port" in camera:
        fields["rtsp_port"] = _require_port(camera["rtsp_port"], "rtsp_port", path)
    if "tls" in camera:
        fields["tls"] = _require_bool(camera["tls"], "tls", path)
    if "rtsp_tls" in camera:
        fields["rtsp_tls"] = _require_bool(camera["rtsp_tls"], "rtsp_tls", path)
    if "tls_verify" in camera:
        fields["tls_verify"] = _require_bool(camera["tls_verify"], "tls_verify", path)
    return fields


def _require_string(value: object, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigFileError(f"{path}: camera.{field} must be a string")
    return value


def _require_port(value: object, field: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigFileError(f"{path}: camera.{field} must be an integer port")
    if not 1 <= value <= 65535:
        raise ConfigFileError(f"{path}: camera.{field} must be between 1 and 65535 (got {value})")
    return value


def _require_bool(value: object, field: str, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigFileError(f"{path}: camera.{field} must be true or false")
    return value


def resolve_camera(
    env: Mapping[str, str], fields: CameraFields
) -> tuple[CameraSettings, list[str]]:
    """Merge the file's fields over HIK_* fallbacks.

    Returns the resolved settings and the visible precedence notes: one line per
    field the file set that disagreed with the environment it overrode, plus a
    line when TLS has verification switched off.

    ``rtsp_tls`` is independent of ``tls`` and defaults to it: omitting the key
    keeps the two services on the same scheme (every pre-existing file behaves
    exactly as before), while setting it lets HTTPS ISAPI pair with plain RTSP
    on cameras that do not serve RTSPS.
    """
    notes: list[str] = []
    host = _merge_str("host", env.get("HIK_HOST"), fields.get("host"), notes)
    user = _merge_str("user", env.get("HIK_USER"), fields.get("user"), notes)
    password = _merge_str("password", env.get("HIK_PASSWORD"), fields.get("password"), notes)
    http_port = _merge_int("http_port", env.get("HIK_HTTP_PORT"), fields.get("http_port"), notes)
    rtsp_port = _merge_int("rtsp_port", env.get("HIK_RTSP_PORT"), fields.get("rtsp_port"), notes)
    tls = fields.get("tls", DEFAULT_TLS)
    rtsp_tls = fields.get("rtsp_tls", tls)
    tls_verify = fields.get("tls_verify", DEFAULT_TLS_VERIFY)
    if (tls or rtsp_tls) and not tls_verify:
        notes.append("camera TLS: certificate verification is off (tls_verify: false)")
    settings = CameraSettings(host, user, password, http_port, rtsp_port, tls, rtsp_tls, tls_verify)
    return settings, notes


def _merge_str(field: str, env_value: str | None, file_value: str | None, notes: list[str]) -> str:
    """The file's value when it set one, else HIK_* (or the built-in default)."""
    if file_value is None:
        return env_value if env_value is not None else _STR_DEFAULTS[field]
    if env_value is not None and env_value != file_value:
        env_name = _STR_FALLBACKS[field]
        if field in _SECRET_FIELDS:
            notes.append(f"camera.{field}: config file value wins over {env_name} (not shown)")
        else:
            notes.append(
                f'camera.{field}: config file value "{file_value}" wins over {env_name}="{env_value}"'
            )
    return file_value


def _merge_int(field: str, env_raw: str | None, file_value: int | None, notes: list[str]) -> int:
    """The file's port when it set one, else HIK_* (or the built-in default)."""
    env_value = _lax_int(env_raw, _INT_DEFAULTS[field])
    if file_value is None:
        return env_value
    if env_raw is not None and env_value != file_value:
        notes.append(
            f"camera.{field}: config file value {file_value} wins over "
            f"{_INT_FALLBACKS[field]}={env_raw}"
        )
    return file_value


def _lax_int(raw: str | None, default: int) -> int:
    """Parse a HIK_* port the way config.py always has (garbage -> default)."""
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def isapi_base(settings: CameraSettings) -> str:
    scheme = "https" if settings.tls else "http"
    return f"{scheme}://{settings.host}:{settings.http_port}"


def rtsp_base(settings: CameraSettings) -> str:
    scheme = "rtsps" if settings.rtsp_tls else "rtsp"
    return f"{scheme}://{settings.host}:{settings.rtsp_port}"
