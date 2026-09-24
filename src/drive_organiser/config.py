"""Environment and TOML configuration.

Env and file config are loaded separately on purpose: `list-folders` needs only
credentials, and it is the command you run *before* config.toml exists.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")


class ConfigError(Exception):
    """Configuration is missing or malformed. Always fatal, always explains itself."""


@dataclass(frozen=True)
class EnvSettings:
    service_account_file: Path
    gemini_api_key: str
    gemini_model: str
    smtp_user: str
    smtp_app_password: str
    mail_to: str
    log_level: str


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class DriveConfig:
    # The first inbox is the primary: `check` looks beside it for unlisted folders.
    inbox_folder_ids: tuple[str, ...]
    unsorted_folder_id: str
    destinations: tuple[Destination, ...]
    gemini_model: str | None
    min_confidence: float
    max_files_per_run: int
    max_inline_bytes: int
    email_on_empty: bool

    def folder_ids(self) -> dict[str, str]:
        """Every configured folder id -> a human label, for validation messages."""
        ids = {i: "inbox" for i in self.inbox_folder_ids}
        ids[self.unsorted_folder_id] = "_Unsorted"
        for d in self.destinations:
            ids[d.id] = d.name
        return ids


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_env(*, require_smtp: bool = True) -> EnvSettings:
    """Read .env plus the process environment. Real env vars win over .env."""
    from dotenv import load_dotenv

    load_dotenv(override=False)

    key_path = Path(_require("GOOGLE_SERVICE_ACCOUNT_FILE"))
    if not key_path.is_file():
        raise ConfigError(
            f"Service account key not found at {key_path}. "
            "Download the JSON key from the Google Cloud console and save it there."
        )

    return EnvSettings(
        service_account_file=key_path,
        gemini_api_key=_require("GEMINI_API_KEY"),
        # No default: model ids move faster than this code does.
        gemini_model=_require("GEMINI_MODEL"),
        smtp_user=_require("SMTP_USER") if require_smtp else os.environ.get("SMTP_USER", ""),
        smtp_app_password=(
            _require("SMTP_APP_PASSWORD") if require_smtp else os.environ.get("SMTP_APP_PASSWORD", "")
        ),
        mail_to=_require("MAIL_TO") if require_smtp else os.environ.get("MAIL_TO", ""),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> DriveConfig:
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Run 'drive-organiser list-folders' and paste its "
            "output into config.toml (see config.example.toml)."
        )
    return parse_config(path.read_text(encoding="utf-8"), source=str(path))


def _inbox_ids(drive: dict, source: str) -> tuple[str, ...]:
    """inbox_folder_ids is a list. The older single inbox_folder_id still loads."""
    if "inbox_folder_ids" in drive and "inbox_folder_id" in drive:
        raise ConfigError(f"{source}: set [drive].inbox_folder_ids or inbox_folder_id, not both.")
    raw = drive.get("inbox_folder_ids", drive.get("inbox_folder_id", ""))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ConfigError(f"{source}: [drive].inbox_folder_ids must be a list of folder ids.")
    ids = tuple(str(i).strip() for i in raw)
    if not ids or not all(ids):
        raise ConfigError(f"{source}: [drive].inbox_folder_ids is missing or holds an empty id.")
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{source}: an inbox id appears more than once in [drive].inbox_folder_ids.")
    return ids


def parse_config(text: str, *, source: str = "<string>") -> DriveConfig:
    """Split out from load_config so it is testable without touching the disk."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from exc

    drive = raw.get("drive")
    if not isinstance(drive, dict):
        raise ConfigError(f"{source} is missing the [drive] section.")

    inboxes = _inbox_ids(drive, source)
    if not str(drive.get("unsorted_folder_id", "")).strip():
        raise ConfigError(f"{source}: [drive].unsorted_folder_id is missing or empty.")

    raw_dests = raw.get("destinations") or []
    if not raw_dests:
        raise ConfigError(
            f"{source} has no [[destinations]]. Add at least one, or every file "
            "would be filed to _Unsorted."
        )

    destinations: list[Destination] = []
    seen: set[str] = set()
    for i, d in enumerate(raw_dests):
        for key in ("id", "name", "description"):
            if not str(d.get(key, "")).strip():
                raise ConfigError(f"{source}: [[destinations]] #{i + 1} is missing '{key}'.")
        if d["id"] in seen:
            raise ConfigError(f"{source}: destination id {d['id']} appears more than once.")
        seen.add(d["id"])
        destinations.append(
            Destination(id=d["id"].strip(), name=d["name"].strip(), description=d["description"].strip())
        )

    unsorted = drive["unsorted_folder_id"].strip()
    for inbox in inboxes:
        if inbox in seen:
            raise ConfigError(
                f"{source}: inbox {inbox} is also listed as a destination. Files would never leave it."
            )
        if inbox == unsorted:
            raise ConfigError(f"{source}: inbox {inbox} is also _Unsorted. Low-confidence files would loop.")
    if unsorted in seen:
        raise ConfigError(f"{source}: _Unsorted is also listed as a destination. Remove it from [[destinations]].")

    gemini = raw.get("gemini", {})
    limits = raw.get("limits", {})
    email = raw.get("email", {})

    confidence = float(gemini.get("min_confidence", 0.6))
    if not 0.0 <= confidence <= 1.0:
        raise ConfigError(f"{source}: [gemini].min_confidence must be between 0 and 1.")

    return DriveConfig(
        inbox_folder_ids=inboxes,
        unsorted_folder_id=unsorted,
        destinations=tuple(destinations),
        gemini_model=(gemini.get("model") or "").strip() or None,
        min_confidence=confidence,
        max_files_per_run=int(limits.get("max_files_per_run", 50)),
        max_inline_bytes=int(limits.get("max_inline_bytes", 20_000_000)),
        email_on_empty=bool(email.get("email_on_empty", False)),
    )
