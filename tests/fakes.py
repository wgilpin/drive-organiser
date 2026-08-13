"""In-memory stand-ins for Drive and Gemini. No network, ever.

FakeDrive records every mutating call so tests can assert a dry run made none.
"""

from __future__ import annotations

from dataclasses import replace

from drive_organiser.drive import FOLDER_MIME, DriveFile
from drive_organiser.gemini import FileDecision, GeminiNamer, Proposal

FULL_CAPS = {
    "canRename": True,
    "canEdit": True,
    "canDownload": True,
    "canMoveItemWithinDrive": True,
    "canAddChildren": True,
}


def make_file(
    file_id: str = "f1",
    name: str = "scan001.pdf",
    mime_type: str = "application/pdf",
    parents: tuple[str, ...] = ("inbox1",),
    caps: dict | None = None,
    size: int | None = 1000,
) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=name,
        mime_type=mime_type,
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        parents=parents,
        capabilities=dict(FULL_CAPS if caps is None else caps),
        size=size,
        modified_time="2026-08-01T00:00:00Z",
    )


def make_folder(folder_id: str, name: str, parent: str = "root1") -> DriveFile:
    return make_file(folder_id, name, FOLDER_MIME, (parent,), size=None)


class FakeDrive:
    """Implements only the DriveClient surface the organiser actually uses."""

    def __init__(self, children: list[DriveFile] | None = None, content: bytes = b"%PDF-1.4 fake"):
        self.children = children if children is not None else [make_file()]
        self.content = content
        self.account_email = "fake@example.iam.gserviceaccount.com"
        # Every mutation lands here. Tests assert this is empty for a dry run.
        self.mutations: list[tuple[str, str, str]] = []
        # (file id, parents the call detached it from) — proves removeParents is right.
        self.detached: list[tuple[str, tuple[str, ...]]] = []
        self.downloads: list[str] = []

    def list_children(self, folder_id: str) -> list[DriveFile]:
        return [f for f in self.children if folder_id in f.parents]

    def child_names(self, folder_id: str) -> set[str]:
        return {f.name for f in self.list_children(folder_id)}

    def walk_files(self, folder_id: str, *, max_depth: int = 5) -> list[tuple[DriveFile, str]]:
        found: list[tuple[DriveFile, str]] = []
        queue: list[tuple[str, str, int]] = [(folder_id, "", 0)]
        seen: set[str] = set()
        while queue:
            current, prefix, depth = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for child in self.list_children(current):
                if child.is_shortcut:
                    continue
                if child.is_folder:
                    if depth < max_depth:
                        sub = f"{prefix}/{child.name}" if prefix else child.name
                        queue.append((child.id, sub, depth + 1))
                else:
                    found.append((child, prefix))
        return found

    def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self.content

    def export(self, file_id: str, mime_type: str) -> bytes:
        self.downloads.append(file_id)
        return self.content

    def rename_and_move(self, file: DriveFile, new_name: str, dest_folder_id: str) -> DriveFile:
        self.mutations.append((file.id, new_name, dest_folder_id))
        self.detached.append((file.id, file.parents))
        return replace(file, name=new_name, parents=(dest_folder_id,))


class FakeNamer:
    """Returns a scripted decision, then routes it through the real gate logic."""

    def __init__(self, filename="Proposed Name", folder_id="acad", confidence=0.9, raises=None):
        self.decision = FileDecision(
            filename=filename, folder_id=folder_id, confidence=confidence, reason="because"
        )
        self.raises = raises
        self.calls = 0

    def propose(self, file, payload, destinations, min_confidence) -> Proposal:
        self.calls += 1
        if self.raises:
            raise self.raises
        return GeminiNamer._route(self.decision, destinations, min_confidence)
