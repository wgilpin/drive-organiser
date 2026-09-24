"""Per-file pipeline: propose a name and folder, then file the document.

A run writes unless `dry_run` is set. Dry run is a debugging tool, not a gate:
nothing configures it, and only an explicit --dry-run on the command line asks
for it.

Idempotency is structural rather than tracked. A filed file leaves the inbox and
is never seen again; a file whose move failed stays put and is retried next run.
One unreadable or unmovable file cannot abort the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import DriveConfig
from .drive import DriveClient, DriveFile
from .extract import extract
from .gemini import GeminiNamer, Proposal
from .naming import build_name, dedupe

log = logging.getLogger(__name__)

# moved   - renamed and re-parented in Drive
# dry_run - a proposal only; --dry-run was passed and nothing was written
# skipped - the service account cannot act on this file
# failed  - something raised; the file is untouched and stays in the inbox
MOVED, DRY_RUN, SKIPPED, FAILED = "moved", "dry_run", "skipped", "failed"

# Internal only: a proposal that is ready to apply. It never reaches a report,
# because every READY outcome becomes MOVED or FAILED before the run returns.
_READY = "ready"


@dataclass
class FileOutcome:
    file: DriveFile
    status: str
    # Captured at construction: after a move, file.name holds the *new* name.
    original_name: str = ""
    new_name: str = ""
    dest_id: str = ""
    dest_name: str = ""
    reason: str = ""
    confidence: float = 0.0
    unsorted: bool = False
    note: str = ""
    error: str = ""
    # Subfolder of the inbox the file was found in. Report only; never a routing hint.
    source_folder: str = ""

    def __post_init__(self) -> None:
        if not self.original_name:
            self.original_name = self.file.name

    @property
    def link(self) -> str:
        return self.file.web_view_link


@dataclass
class RunResult:
    outcomes: list[FileOutcome]
    truncated: int = 0
    dry_run: bool = False

    def by_status(self, status: str) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def _destination_names(cfg: DriveConfig) -> dict[str, str]:
    names = {d.id: d.name for d in cfg.destinations}
    names[cfg.unsorted_folder_id] = "_Unsorted"
    return names


def organise(
    drive: DriveClient, namer: GeminiNamer, cfg: DriveConfig, *, dry_run: bool = False
) -> RunResult:
    # Files nested in subfolders of an inbox count too. The subfolder is ignored:
    # it is neither moved nor used as a hint about where the file belongs.
    found = [hit for inbox in cfg.inbox_folder_ids for hit in drive.walk_files(inbox)]
    files = [f for f, _ in found]
    source_of = {f.id: path for f, path in found}

    truncated = 0
    if len(files) > cfg.max_files_per_run:
        truncated = len(files) - cfg.max_files_per_run
        log.warning("inbox holds %d files; this run covers %d", len(files), cfg.max_files_per_run)
        files = files[: cfg.max_files_per_run]

    names = _destination_names(cfg)
    # Reserve names per destination so two files in one run cannot collide.
    taken: dict[str, set[str]] = {}
    outcomes: list[FileOutcome] = []

    for file in files:
        try:
            outcome = _plan_one(drive, namer, cfg, file, names, taken)
            if outcome.status == _READY:
                if dry_run:
                    outcome.status = DRY_RUN
                else:
                    _apply(drive, outcome)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            log.exception("failed on %s", file.name)
            outcome = FileOutcome(file=file, status=FAILED, error=str(exc))
        outcome.source_folder = source_of.get(file.id, "")
        outcomes.append(outcome)

    return RunResult(outcomes=outcomes, truncated=truncated, dry_run=dry_run)


def _plan_one(
    drive: DriveClient,
    namer: GeminiNamer,
    cfg: DriveConfig,
    file: DriveFile,
    names: dict[str, str],
    taken: dict[str, set[str]],
) -> FileOutcome:
    if not (file.can("canRename") and file.can("canMoveItemWithinDrive")):
        return FileOutcome(
            file=file, status=SKIPPED, note="the service account cannot rename or move this file"
        )

    payload = extract(drive, file, cfg.max_inline_bytes)
    proposal: Proposal = namer.propose(file, payload, cfg.destinations, cfg.min_confidence)

    dest_id = cfg.unsorted_folder_id if proposal.routed_to_unsorted else proposal.decision.folder_id
    if dest_id not in taken:
        taken[dest_id] = drive.child_names(dest_id)

    new_name = dedupe(
        build_name(
            proposal.decision.filename,
            file.name,
            mime_type=file.mime_type,
            is_native_google=file.is_native_google,
        ),
        taken[dest_id],
    )
    taken[dest_id].add(new_name)

    return FileOutcome(
        file=file,
        status=_READY,
        new_name=new_name,
        dest_id=dest_id,
        dest_name=names.get(dest_id, dest_id),
        reason=proposal.decision.reason,
        confidence=proposal.decision.confidence,
        unsorted=proposal.routed_to_unsorted,
        # Both, not either: "model chose _Unsorted" alone hides that the model
        # saw only metadata because the file was too big to send.
        note="; ".join(n for n in (proposal.routing_note, payload.note) if n),
    )


def _apply(drive: DriveClient, outcome: FileOutcome) -> None:
    """The only call in this package that changes anything in Drive."""
    outcome.file = drive.rename_and_move(outcome.file, outcome.new_name, outcome.dest_id)
    outcome.status = MOVED
