"""Thin wrapper over the Drive v3 API.

Two rules encoded here, both from the Phase 1 spike:
  * Nothing in this module creates or deletes anything. A service account has no
    storage quota, so it cannot own files; `canDelete` is False in any case.
  * `fields` is always explicit — Drive v3 returns a minimal set otherwise, and
    `webViewLink` (needed for the email links) is not in it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

FILE_FIELDS = "id,name,mimeType,webViewLink,parents,capabilities,size,modifiedTime"
FOLDER_FIELDS = "id,name,parents,capabilities"


class DriveError(Exception):
    """A Drive call failed in a way worth reporting to the user verbatim."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    web_view_link: str
    parents: tuple[str, ...]
    capabilities: dict
    size: int | None
    modified_time: str

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == SHORTCUT_MIME

    @property
    def is_native_google(self) -> bool:
        return self.mime_type.startswith("application/vnd.google-apps.")

    def can(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))


def _to_file(raw: dict) -> DriveFile:
    size = raw.get("size")
    return DriveFile(
        id=raw["id"],
        name=raw.get("name", ""),
        mime_type=raw.get("mimeType", ""),
        web_view_link=raw.get("webViewLink", ""),
        parents=tuple(raw.get("parents", ())),
        capabilities=raw.get("capabilities", {}) or {},
        size=int(size) if size is not None else None,
        modified_time=raw.get("modifiedTime", ""),
    )


class DriveClient:
    def __init__(self, service_account_file: Path):
        creds = service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=SCOPES
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.account_email = creds.service_account_email

    # --- reads -------------------------------------------------------------

    def _paged(self, *, q: str, fields: str) -> list[dict]:
        out: list[dict] = []
        token: str | None = None
        while True:
            resp = (
                self._svc.files()
                .list(
                    q=q,
                    fields=f"nextPageToken,files({fields})",
                    pageSize=100,
                    pageToken=token,
                    orderBy="name",
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def list_folders(self) -> list[DriveFile]:
        """Every folder the service account can see, i.e. inside the shared ancestor."""
        raw = self._paged(q=f"mimeType='{FOLDER_MIME}' and trashed=false", fields=FOLDER_FIELDS)
        return [_to_file(r | {"mimeType": FOLDER_MIME}) for r in raw]

    def list_children(self, folder_id: str) -> list[DriveFile]:
        raw = self._paged(q=f"'{folder_id}' in parents and trashed=false", fields=FILE_FIELDS)
        return [_to_file(r) for r in raw]

    def walk_files(self, folder_id: str, *, max_depth: int = 5) -> list[tuple[DriveFile, str]]:
        """Every file at or below folder_id, with the path of its containing folder.

        Subfolders of the inbox are traversed but never themselves moved, and the
        service account cannot delete the emptied folders left behind. The path is
        for the report only; it plays no part in the choice of destination.
        """
        found: list[tuple[DriveFile, str]] = []
        queue: list[tuple[str, str, int]] = [(folder_id, "", 0)]
        seen: set[str] = set()

        while queue:
            current_id, prefix, depth = queue.pop(0)
            if current_id in seen:  # a cycle is impossible in Drive, but cheap to rule out
                continue
            seen.add(current_id)

            for child in self.list_children(current_id):
                if child.is_shortcut:
                    continue
                if child.is_folder:
                    if depth < max_depth:
                        sub = f"{prefix}/{child.name}" if prefix else child.name
                        queue.append((child.id, sub, depth + 1))
                    else:
                        log.warning("not descending past depth %d into %s", max_depth, child.name)
                else:
                    found.append((child, prefix))
        return found

    def child_names(self, folder_id: str) -> set[str]:
        """Existing names in a folder, for collision-avoidance when renaming."""
        return {f.name for f in self.list_children(folder_id)}

    def get(self, file_id: str) -> DriveFile:
        try:
            raw = self._svc.files().get(fileId=file_id, fields=FILE_FIELDS).execute()
        except HttpError as exc:
            raise DriveError(f"could not read {file_id}: {_explain(exc)}") from exc
        return _to_file(raw)

    def download(self, file_id: str) -> bytes:
        """Raw bytes of a binary file. Not valid for native Google types."""
        try:
            return self._svc.files().get_media(fileId=file_id).execute()
        except HttpError as exc:
            raise DriveError(_explain(exc)) from exc

    def export(self, file_id: str, mime_type: str) -> bytes:
        """Converted bytes of a native Google file. Drive caps exports at 10 MB."""
        try:
            return self._svc.files().export(fileId=file_id, mimeType=mime_type).execute()
        except HttpError as exc:
            raise DriveError(_explain(exc)) from exc

    # --- the one mutation --------------------------------------------------

    def rename_and_move(self, file: DriveFile, new_name: str, dest_folder_id: str) -> DriveFile:
        """Rename and re-parent in a single call, so there is no half-applied state."""
        try:
            raw = (
                self._svc.files()
                .update(
                    fileId=file.id,
                    body={"name": new_name},
                    addParents=dest_folder_id,
                    removeParents=",".join(file.parents),
                    fields=FILE_FIELDS,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveError(f"could not file '{file.name}': {_explain(exc)}") from exc
        return _to_file(raw)


def _explain(exc: HttpError) -> str:
    status = getattr(exc, "status_code", None) or exc.resp.status
    reason = getattr(exc, "reason", "") or ""
    body = str(exc)
    if status == 403 and "storageQuota" in body:
        return "403 storageQuotaExceeded — a service account cannot own files; nothing here creates one"
    if "exportSizeLimitExceeded" in body:
        return "export exceeds Drive's 10 MB conversion limit"
    if status == 404:
        return "404 not found — is the folder shared with the service account?"
    return f"{status} {reason}".strip()
