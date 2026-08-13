"""Decide what to send Gemini for a given file, then fetch it.

`plan_extraction` is pure and holds every routing rule, so the whole decision
table is testable without a network. `extract` does the IO and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .drive import DriveClient, DriveError, DriveFile

# Drive refuses to export a native Google file above this size.
EXPORT_LIMIT = 10_000_000

# Native Google types -> (export mime, how the result is handled)
GOOGLE_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", "text"),
    # CSV export covers the first sheet only. Enough to name a file by.
    "application/vnd.google-apps.spreadsheet": ("text/csv", "text"),
    "application/vnd.google-apps.presentation": ("application/pdf", "bytes"),
}

TEXT_MIMES = ("text/",)
TEXT_EXACT = {"application/json", "application/xml", "application/x-yaml"}
INLINE_BYTE_MIMES = ("image/",)
INLINE_BYTE_EXACT = {"application/pdf"}
OOXML = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

MAX_TEXT_CHARS = 20_000

Action = Literal["export", "download", "metadata", "skip"]


@dataclass(frozen=True)
class ExtractionPlan:
    action: Action
    export_mime: str | None = None
    as_kind: str = "metadata"
    reason: str = ""


@dataclass(frozen=True)
class Payload:
    kind: Literal["text", "bytes", "metadata"]
    text: str | None = None
    data: bytes | None = None
    mime_type: str | None = None
    note: str = ""


def plan_extraction(mime_type: str, size: int | None, max_inline_bytes: int) -> ExtractionPlan:
    """Pure routing decision. Every branch is covered by tests."""
    if mime_type in ("application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"):
        return ExtractionPlan("skip", reason="not a file")

    if mime_type in GOOGLE_EXPORTS:
        export_mime, kind = GOOGLE_EXPORTS[mime_type]
        return ExtractionPlan("export", export_mime=export_mime, as_kind=kind)

    if mime_type.startswith("application/vnd.google-apps."):
        # Forms, Drawings, Sites, Jamboards: no useful export for naming.
        return ExtractionPlan("metadata", reason=f"no text export for {mime_type}")

    if size is not None and size > max_inline_bytes:
        return ExtractionPlan("metadata", reason=f"{size:,} bytes exceeds the inline limit")

    if mime_type.startswith(TEXT_MIMES) or mime_type in TEXT_EXACT:
        return ExtractionPlan("download", as_kind="text")

    if mime_type.startswith(INLINE_BYTE_MIMES) or mime_type in INLINE_BYTE_EXACT or mime_type in OOXML:
        return ExtractionPlan("download", as_kind="bytes")

    return ExtractionPlan("metadata", reason=f"unsupported type {mime_type}")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]


def extract(drive: DriveClient, file: DriveFile, max_inline_bytes: int) -> Payload:
    """Fetch whatever the plan calls for. Never raises: falls back to metadata."""
    plan = plan_extraction(file.mime_type, file.size, max_inline_bytes)

    if plan.action in ("skip", "metadata"):
        return Payload(kind="metadata", note=plan.reason)

    if not file.can("canDownload"):
        return Payload(kind="metadata", note="no download permission")

    try:
        if plan.action == "export":
            if file.size is not None and file.size > EXPORT_LIMIT:
                return Payload(kind="metadata", note="too large to export (10 MB limit)")
            raw = drive.export(file.id, plan.export_mime or "text/plain")
        else:
            raw = drive.download(file.id)
    except DriveError as exc:
        return Payload(kind="metadata", note=f"could not read content: {exc}")

    if plan.as_kind == "text":
        text = _decode(raw)
        if not text.strip():
            return Payload(kind="metadata", note="file is empty")
        return Payload(kind="text", text=text)

    mime = plan.export_mime or file.mime_type
    return Payload(kind="bytes", data=raw, mime_type=mime)
