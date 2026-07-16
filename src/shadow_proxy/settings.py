"""Runtime settings.

Two layers:

- Secrets & environment come from ``.env`` / environment variables (``Settings``).
- Non-secret behaviour (routes, models, dispatcher, evaluator) comes from a YAML
  config file (``AppConfig``).

Secret fields (``do_inference_api_key``, ``do_spaces_secret``) are typed as
:class:`pydantic.SecretStr` so they cannot accidentally be printed, logged, or
serialized. Their real value is only obtainable via ``.get_secret_value()``.

Startup validation lives in :meth:`Settings.assert_runtime_ready` — the app
factory calls it during ``lifespan`` startup so a deployment with a missing or
dummy key **refuses to serve traffic** rather than failing on the first
customer request.
"""

from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)

# Substring markers used to detect resolved paths that leak into the Python
# install prefix (e.g. ``/opt/venv/lib/python3.13/...``). Landing inside the
# site-packages tree is always a bug — either a relative path anchored to the
# wrong dir, or a package-internal resource being written to. See
# :meth:`Settings.warn_on_suspicious_paths` for the loud-and-clear diagnostic.
_INSTALL_PREFIX_MARKERS: tuple[str, ...] = (
    "/site-packages/",
    "/opt/venv/",
    "/dist-packages/",
)

# Module-level record of where .env was actually loaded from (populated by
# ``get_settings``). Exposed via :meth:`Settings.env_file_path` so main.py can
# log it and /v1/config can surface it for debugging.
_LOADED_ENV_FILE: Path | None = None


def _find_env_file() -> Path | None:
    """Locate a ``.env`` file to load.

    Resolution order (first match wins):

    1. ``$SHADOW_PROXY_ENV_FILE`` — explicit absolute path override.
    2. Walk **up** from ``$PWD`` looking for a ``.env`` alongside a
       ``pyproject.toml`` (so nested subdirs still find the project's ``.env``).
    3. A ``.env`` sibling of the ``settings.py`` module's project root (works
       out-of-the-box in Docker where the process starts in ``/app`` and the
       ``.env`` sits in the repo root).

    Returns ``None`` if no ``.env`` is found (that's fine — env vars alone are
    enough, and the runtime validator will complain loudly if secrets are
    missing).
    """
    override = os.environ.get("SHADOW_PROXY_ENV_FILE")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file():
            return p

    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
        if (parent / "pyproject.toml").is_file():
            break

    module_root = Path(__file__).resolve().parents[2]
    candidate = module_root / ".env"
    if candidate.is_file():
        return candidate

    return None

# Substrings that indicate a placeholder / dummy key. Matched case-insensitively
# against the API key. Being conservative here is intentional — a false positive
# on a real key would trip within seconds of deployment; a false negative would
# send garbage to DO for potentially every customer request.
_DUMMY_KEY_MARKERS: tuple[str, ...] = (
    "fake",
    "dummy",
    "example",
    "placeholder",
    "changeme",
    "change-me",
    "your-key",
    "your_key",
    "yourkey",
    "todo",
    "xxx",
    "abc123",
    "secret-here",
    "paste-",
)

# Minimum plausible key length. DO Model Access Keys are considerably longer,
# but we don't want to hard-code a specific prefix (which is a documented
# implementation detail that could change). 20 catches typos and empty-ish
# values without being brittle.
_MIN_KEY_LENGTH = 20


class Settings(BaseSettings):
    """Environment / secret settings.

    Never construct a :class:`DOInferenceClient` from raw env vars; always
    thread them through :class:`Settings` so the validation below applies.
    """

    # NOTE: env_file is set dynamically by ``get_settings`` via
    # :func:`_find_env_file` so ``.env`` resolves reliably regardless of CWD.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- DO Serverless Inference ---
    do_inference_base_url: str = "https://inference.do-ai.run/v1"
    do_inference_api_key: SecretStr = SecretStr("")

    # --- Proxy auth (bearer tokens accepted by /v1/chat, comma-separated) ---
    proxy_api_keys: str = ""

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///./data/comparisons.db"

    # --- Raw payload store ---
    raw_store_type: Literal["filesystem", "spaces"] = "filesystem"
    raw_store_filesystem_path: str = "./data/raw"
    do_spaces_bucket: str = ""
    do_spaces_region: str = "nyc3"
    do_spaces_endpoint_url: str = "https://nyc3.digitaloceanspaces.com"
    do_spaces_key: str = ""
    do_spaces_secret: SecretStr = SecretStr("")

    # --- Observability ---
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- Config file ---
    config_file: str = "./config/models.yaml"

    # --- Explicit opt-out for local dev / UI-only work ---
    # Setting this to true (env var SHADOW_PROXY_ALLOW_DUMMY_KEY=1) lets the
    # server boot without a valid DO key. Real requests to DO will still fail,
    # but you can iterate on the UI/schema/tests without a real key.
    shadow_proxy_allow_dummy_key: bool = False

    @field_validator("do_inference_base_url")
    @classmethod
    def _base_url_must_be_https(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("DO_INFERENCE_BASE_URL must include scheme (http/https)")
        return v.rstrip("/")

    @property
    def proxy_api_key_set(self) -> set[str]:
        return {k.strip() for k in self.proxy_api_keys.split(",") if k.strip()}

    def assert_runtime_ready(self) -> None:
        """Verify all secrets are present and look real. Call at startup.

        Raises ``RuntimeError`` with a human-actionable message if:

        - The DO inference key is missing.
        - The DO inference key looks like a placeholder / dummy value.
        - The DO inference key is implausibly short.
        - ``raw_store_type == "spaces"`` but the Spaces creds are incomplete.

        Bypass only in local dev by exporting
        ``SHADOW_PROXY_ALLOW_DUMMY_KEY=1``.
        """
        if self.shadow_proxy_allow_dummy_key:
            return

        key = self.do_inference_api_key.get_secret_value()
        if not key:
            raise RuntimeError(_key_missing_message())

        lowered = key.lower()
        for marker in _DUMMY_KEY_MARKERS:
            if marker in lowered:
                raise RuntimeError(_dummy_key_message(marker))

        if len(key) < _MIN_KEY_LENGTH:
            raise RuntimeError(_short_key_message(len(key)))

        if self.raw_store_type == "spaces":
            missing = [
                name
                for name, val in (
                    ("DO_SPACES_BUCKET", self.do_spaces_bucket),
                    ("DO_SPACES_KEY", self.do_spaces_key),
                    ("DO_SPACES_SECRET", self.do_spaces_secret.get_secret_value()),
                )
                if not val
            ]
            if missing:
                raise RuntimeError(
                    "RAW_STORE_TYPE=spaces but these env vars are unset: "
                    + ", ".join(missing)
                )

    def redacted_key_summary(self) -> str:
        """Return a safe-to-log summary of the DO key (length only, no value)."""
        key = self.do_inference_api_key.get_secret_value()
        if not key:
            return "unset"
        return f"length={len(key)}"

    def env_file_path(self) -> str | None:
        """Return the resolved ``.env`` path used to populate this instance."""
        return str(_LOADED_ENV_FILE) if _LOADED_ENV_FILE is not None else None

    # ------------------------------------------------------------------
    # Path anchoring
    #
    # Relative paths in .env / env vars (config_file, raw_store_filesystem_path,
    # sqlite DATABASE_URL) need a base directory to resolve against. We walk
    # a fixed precedence list of candidate anchors and pick the first that
    # exists — the goal is that the app behaves identically whether it's
    # launched from the project root, from ``/``, or from Docker's WORKDIR,
    # and NEVER ends up anchored inside the Python install prefix
    # (``/opt/venv/lib/pythonX.Y/site-packages/...``), which is the class of
    # bug that caused the DO App Platform outage.
    #
    # Precedence (first hit wins):
    #   1. Directory of the loaded ``.env`` (portable dev + explicit override).
    #   2. ``$PWD`` if it contains a ``pyproject.toml`` OR a ``config/`` dir
    #      (matches ``uvicorn`` launched from the repo root AND the Docker
    #      layout where WORKDIR=/app and /app/config exists).
    #   3. ``/app`` (Docker/PaaS runtime convention).
    #   4. The installed package's own directory (``.../shadow_proxy/``) as a
    #      last resort — safe for read-only resource lookups only. Writable
    #      paths (DB, raw store) should always be driven by absolute-value
    #      env vars in prod; :meth:`warn_on_suspicious_paths` shouts if a
    #      writable path ends up under the install prefix.
    # ------------------------------------------------------------------
    def _anchor_dir(self) -> Path:
        if _LOADED_ENV_FILE is not None:
            return _LOADED_ENV_FILE.parent

        cwd = Path.cwd().resolve()
        if (cwd / "pyproject.toml").is_file() or (cwd / "config").is_dir():
            return cwd

        app_dir = Path("/app")
        if app_dir.is_dir():
            return app_dir

        # Last-resort fallback: the installed package directory itself
        # (``.../site-packages/shadow_proxy/``). Only safe for read-only
        # resource lookups — writable paths landing here will trip the
        # startup warning in :meth:`warn_on_suspicious_paths`.
        return Path(__file__).resolve().parent

    def _resolve_relative(self, raw: str) -> Path:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self._anchor_dir() / p
        return p.resolve()

    def resolved_config_file(self) -> Path:
        return self._resolve_relative(self.config_file)

    def resolved_raw_store_path(self) -> Path:
        return self._resolve_relative(self.raw_store_filesystem_path)

    def resolved_database_url(self) -> str:
        """Rewrite a SQLite URL's relative file path to an absolute one."""
        url = self.database_url
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if url.startswith(prefix):
                path_part = url[len(prefix):]
                p = Path(path_part).expanduser()
                if not p.is_absolute():
                    p = self._anchor_dir() / p
                return f"{prefix}{p.resolve()}"
        return url

    def _sqlite_db_path(self) -> Path | None:
        """Return the resolved on-disk SQLite path, or None if non-SQLite."""
        url = self.resolved_database_url()
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if url.startswith(prefix):
                return Path(url[len(prefix):])
        return None

    def warn_on_suspicious_paths(self) -> list[str]:
        """Log a warning for any resolved path that leaks into the Python
        install prefix (``/opt/venv/...``, ``.../site-packages/...``).

        This condition is always a bug — it means a relative path was
        resolved against the installed package directory instead of an
        actual data / config location. We log at WARNING so the noise is
        visible in prod logs but doesn't hard-fail the boot (the caller may
        have supplied a bundled default that's fine to read).

        Returns the list of formatted warning messages emitted (empty when
        all resolved paths look sane) — handy for structured logging by the
        caller.
        """
        anchor = self._anchor_dir()
        checks: list[tuple[str, Path]] = [
            ("config_file", self.resolved_config_file()),
            ("raw_store_filesystem_path", self.resolved_raw_store_path()),
        ]
        db_path = self._sqlite_db_path()
        if db_path is not None:
            checks.append(("database_url (sqlite)", db_path))

        warnings: list[str] = []
        for label, path in checks:
            s = str(path)
            if any(marker in s for marker in _INSTALL_PREFIX_MARKERS):
                msg = (
                    f"suspicious resolved path: {label}={s} resolves inside the "
                    f"Python install prefix (anchor_dir={anchor}). This usually "
                    "means an env var is unset AND the CWD has no pyproject.toml "
                    "/ config/ dir AND /app doesn't exist. Set the env var to an "
                    "absolute path (CONFIG_FILE=/app/config/models.yaml, "
                    "RAW_STORE_FILESYSTEM_PATH=/app/data/raw, "
                    "DATABASE_URL=sqlite+aiosqlite:////app/data/comparisons.db)."
                )
                _log.warning(msg)
                warnings.append(msg)
        return warnings


# --- Helper error messages -------------------------------------------------

def _key_missing_message() -> str:
    return (
        "DO_INFERENCE_API_KEY is not set.\n\n"
        "  Create a Model Access Key:\n"
        "    1. https://cloud.digitalocean.com\n"
        "    2. INFERENCE -> Manage -> Model Access Keys\n"
        "    3. 'Create model access key', copy the secret (shown once),\n"
        "       paste it into .env as DO_INFERENCE_API_KEY=...\n\n"
        "  For local dev without a real key, set SHADOW_PROXY_ALLOW_DUMMY_KEY=1."
    )


def _dummy_key_message(marker: str) -> str:
    return (
        f"DO_INFERENCE_API_KEY looks like a placeholder value "
        f"(contains {marker!r}). Refusing to start so we don't send "
        "garbage requests to DigitalOcean. Replace it with a real Model "
        "Access Key from https://cloud.digitalocean.com (INFERENCE -> Manage).\n"
        "  For local dev, set SHADOW_PROXY_ALLOW_DUMMY_KEY=1."
    )


def _short_key_message(length: int) -> str:
    return (
        f"DO_INFERENCE_API_KEY is only {length} characters long, which is too "
        f"short to be a real Model Access Key (expected >= {_MIN_KEY_LENGTH}). "
        "Did you paste the full key?\n"
        "  For local dev, set SHADOW_PROXY_ALLOW_DUMMY_KEY=1."
    )


# ---------- YAML-driven application config ----------


class LLMEndpointConfig(BaseModel):
    model_id: str
    timeout_s: float = 15.0
    max_retries: int = 0


class RouteConfig(BaseModel):
    primary: LLMEndpointConfig
    candidate: LLMEndpointConfig


class EvaluatorConfig(BaseModel):
    require_json: bool = True
    compare_key: str = "action"
    normalize: Literal["none", "lowercase_strip"] = "lowercase_strip"


class DispatcherConfig(BaseModel):
    type: Literal["in_process", "redis_streams", "kafka"] = "in_process"
    queue_capacity: int = 10_000
    workers: int = 8
    overflow_policy: Literal["drop_new", "drop_old"] = "drop_new"

    @field_validator("workers")
    @classmethod
    def _positive_workers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("dispatcher.workers must be >= 1")
        return v


class StoreConfig(BaseModel):
    on_failure: Literal["fail_open", "fail_closed"] = "fail_open"
    sweeper_interval_s: float = 300.0
    stale_threshold_s: float = 600.0


class AppConfig(BaseModel):
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    evaluator: EvaluatorConfig = Field(default_factory=EvaluatorConfig)
    dispatcher: DispatcherConfig = Field(default_factory=DispatcherConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)

    def route(self, name: str) -> RouteConfig:
        try:
            return self.routes[name]
        except KeyError as exc:
            raise KeyError(f"Route '{name}' not defined in config") from exc


def _bundled_config_path() -> Path:
    """Return the on-disk path to the bundled ``models.yaml`` shipped in the
    installed package.

    NOTE: developer-side edits should be made to the top-level
    ``config/models.yaml`` — the copy under ``src/shadow_proxy/_bundled/``
    is the guaranteed-present fallback shipped in the wheel. When you edit
    one, update the other (or add a build hook to auto-sync — see
    pyproject.toml [tool.setuptools.package-data]).
    """
    resource = resources.files("shadow_proxy").joinpath("_bundled/config/models.yaml")
    # importlib.resources returns a Traversable; for regular installs this is
    # a real Path, but we go through as_file() for wheel/zip safety.
    with resources.as_file(resource) as p:
        return Path(p)


def load_app_config(path: str | Path) -> AppConfig:
    """Load YAML app config from ``path`` with a bundled-resource fallback.

    Resolution:

    1. If ``path`` exists on disk, load it (the normal case — set via env
       var ``CONFIG_FILE`` or the ``.env``-anchored default).
    2. Otherwise fall back to the copy shipped inside the installed
       ``shadow_proxy`` package under ``_bundled/config/models.yaml``. This
       is what prevents crashes when the deploy-time filesystem layout
       differs from the developer machine (e.g. a container missing the
       ``/app/config/`` bind-mount, or a PaaS build that didn't copy the
       ``config/`` directory).
    3. If neither exists, raise ``FileNotFoundError`` naming BOTH candidates
       so the operator can immediately see what went wrong.
    """
    primary = Path(path)
    if primary.exists():
        return _parse_config_yaml(primary)

    try:
        bundled = _bundled_config_path()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise FileNotFoundError(
            f"Config file not found: {primary} and no bundled fallback "
            f"is available inside the shadow_proxy package ({exc})."
        ) from exc

    if bundled.exists():
        _log.warning(
            "app_config.fallback_to_bundled requested=%s bundled=%s "
            "(set CONFIG_FILE to an absolute path pointing at your real "
            "config/models.yaml to silence this).",
            primary,
            bundled,
        )
        return _parse_config_yaml(bundled)

    raise FileNotFoundError(
        f"Config file not found. Tried:\n"
        f"  1. requested path : {primary}\n"
        f"  2. bundled fallback: {bundled}\n"
        "Neither path exists. Set CONFIG_FILE to a readable YAML file, or "
        "reinstall the shadow_proxy package to restore the bundled default."
    )


def _parse_config_yaml(p: Path) -> AppConfig:
    data = yaml.safe_load(p.read_text()) or {}
    return AppConfig.model_validate(data)


def get_settings() -> Settings:
    """Build :class:`Settings` with a robustly-resolved ``.env`` path.

    The resolved path is recorded in ``_LOADED_ENV_FILE`` and can be read back
    via :meth:`Settings.env_file_path` for logging / /v1/config output.
    """
    global _LOADED_ENV_FILE  # noqa: PLW0603 — intentional: module-level observability state
    env_file = _find_env_file()
    _LOADED_ENV_FILE = env_file
    if env_file is None:
        return Settings()
    return Settings(_env_file=str(env_file))  # type: ignore[call-arg]
