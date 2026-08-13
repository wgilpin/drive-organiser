"""Command line entry points.

    list-folders   print paste-ready TOML for every folder the SA can see
    check          validate credentials and config without touching any file
    run            file every inbox document, once
    loop           run on the hour, forever (the container entrypoint)

A run writes. Pass --dry-run to see what it would do instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, DriveConfig, load_config, load_env
from .drive import DriveClient, DriveError, DriveFile

log = logging.getLogger("drive_organiser")

# Folders that are machinery, not filing destinations.
RESERVED = {"_Inbox", "_Unsorted"}


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def cmd_list_folders(_args: argparse.Namespace) -> int:
    env = load_env(require_smtp=False)
    drive = DriveClient(env.service_account_file)
    folders = drive.list_folders()

    if not folders:
        print(
            f"No folders visible to {drive.account_email}.\n\n"
            "Either the Drive API is not enabled on the project, or the parent "
            "folder has not been shared with that address as Editor.",
            file=sys.stderr,
        )
        return 1

    by_name = {f.name: f for f in folders}
    print(f"# Folders visible to {drive.account_email}\n")
    print("[drive]")
    for label, key in (("_Inbox", "inbox_folder_id"), ("_Unsorted", "unsorted_folder_id")):
        found = by_name.get(label)
        if found:
            print(f'{key:<20}= "{found.id}"')
        else:
            print(f'{key:<20}= ""  # NOT FOUND: create a folder named {label}')

    # The shared ancestor is a container, not a destination. Filing into it would
    # scatter files next to the machinery folders, so drop it from the suggestions.
    ancestors: set[str] = set()
    for label in RESERVED:
        found = by_name.get(label)
        if found:
            ancestors.update(found.parents)

    print()
    for f in sorted(folders, key=lambda x: x.name):
        if f.name in RESERVED:
            continue
        if f.id in ancestors:
            print(f'# skipped "{_toml_escape(f.name)}" — it is the shared parent folder, not a destination')
            continue
        if not f.can("canAddChildren"):
            print(f'# skipped "{_toml_escape(f.name)}" — no write access for the service account')
            continue
        print("[[destinations]]")
        print(f'id          = "{f.id}"')
        print(f'name        = "{_toml_escape(f.name)}"')
        print('description = ""  # describe what belongs here — Gemini reads this')
        print()

    print("# Delete any folder above that should not be a filing destination,")
    print("# then write a description for each one you keep.", file=sys.stdout)
    return 0


def find_unconfigured(folders: list[DriveFile], cfg: DriveConfig) -> list[DriveFile]:
    """Folders sitting alongside the configured ones but absent from config.toml.

    Only siblings of _Inbox count. A folder nested inside a destination is a
    subfolder of that destination, not a missed one, and reporting it is noise.
    """
    configured = set(cfg.folder_ids())
    inbox = next((f for f in folders if f.id == cfg.inbox_folder_id), None)
    if inbox is None:
        return []
    ancestors = set(inbox.parents)
    return sorted(
        (
            f
            for f in folders
            if f.id not in configured
            and f.id not in ancestors
            and ancestors.intersection(f.parents)
        ),
        key=lambda f: f.name,
    )


def _validate(drive: DriveClient, cfg: DriveConfig) -> list[str]:
    """Check every configured folder resolves and is writable. Returns problems."""
    problems: list[str] = []
    visible = {f.id: f for f in drive.list_folders()}

    for folder_id, label in cfg.folder_ids().items():
        found = visible.get(folder_id)
        if found is None:
            problems.append(
                f"{label}: id {folder_id} is not visible to {drive.account_email}. "
                "Check the id, and that its parent is shared as Editor."
            )
            continue
        if not found.can("canAddChildren"):
            problems.append(f"{label}: '{found.name}' is visible but not writable (canAddChildren=False).")
        if found.name != label and label not in {"inbox", "_Unsorted"}:
            problems.append(f"{label}: renamed in Drive to '{found.name}' — update config.toml.")
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    env = load_env()
    cfg = load_config(args.config)
    drive = DriveClient(env.service_account_file)
    print(f"service account : {drive.account_email}")
    print(f"gemini model    : {cfg.gemini_model or env.gemini_model}")
    print(f"destinations    : {len(cfg.destinations)}")

    folders = drive.list_folders()
    problems = _validate(drive, cfg)
    if problems:
        print("\nPROBLEMS:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    # Advisory, not a failure: an unlisted folder simply never receives files.
    missing = find_unconfigured(folders, cfg)
    if missing:
        print(f"\nWARNING: {len(missing)} folder(s) in Drive are not in {args.config}.")
        print("They receive no files until you add them:")
        for f in missing:
            print(f"  - {f.name}")
        print("Run 'drive-organiser list-folders' to get their ids.")

    # Counts nested files too, or it disagrees with what `run` actually processes.
    waiting = drive.walk_files(cfg.inbox_folder_id)
    nested = sum(1 for _, path in waiting if path)
    detail = f" ({nested} in subfolders)" if nested else ""
    print(f"inbox           : {len(waiting)} file(s) waiting{detail}")

    # Prove SMTP now, not at 3am when a run has already moved files.
    from .mailer import MailError, verify

    try:
        verify(env)
        print(f"smtp            : login OK, reports go to {env.mail_to}")
    except MailError as exc:
        print(f"\nPROBLEM: {exc}", file=sys.stderr)
        return 1
    print("\nAll configured folders resolve and are writable.")
    return 0


STATUS_MARK = {"moved": "OK", "dry_run": "->", "skipped": " -", "failed": " !"}


def cmd_run(args: argparse.Namespace) -> int:
    from .gemini import GeminiNamer
    from .mailer import MailError, send
    from .organiser import FAILED, organise
    from .report import build_subject, render_html, render_text, should_send

    email = not args.no_email
    env = load_env(require_smtp=email)
    cfg = load_config(args.config)
    drive = DriveClient(env.service_account_file)
    namer = GeminiNamer(env.gemini_api_key, cfg.gemini_model or env.gemini_model)

    result = organise(drive, namer, cfg, dry_run=args.dry_run)

    if email and should_send(result, email_on_empty=cfg.email_on_empty):
        try:
            send(env, build_subject(result), render_html(result), render_text(result))
            print(f"summary emailed to {env.mail_to}")
        except MailError as exc:
            # A failed email must not lose the record of what was already moved.
            print(f"\nWARNING: could not send the summary: {exc}", file=sys.stderr)

    if not result.outcomes:
        print("Inbox is empty. Nothing to do.")
        return 0

    banner = "proposals only, nothing moved" if args.dry_run else "FILED"
    print(f"\n{len(result.outcomes)} file(s) — {banner}:\n")
    for o in result.outcomes:
        where = f"  [from {o.source_folder}/]" if o.source_folder else ""
        print(f"{STATUS_MARK.get(o.status, '  ')} {o.original_name}{where}")
        if o.status in ("moved", "dry_run"):
            arrow = "filed as" if o.status == "moved" else "new name"
            print(f"     {arrow} : {o.new_name}")
            print(f"     folder   : {o.dest_name}  (confidence {o.confidence:.2f})")
            print(f"     reason   : {o.reason}")
            if o.note:
                print(f"     note     : {o.note}")
        elif o.status == "skipped":
            print(f"     skipped  : {o.note}")
        else:
            print(f"     error    : {o.error}")
        print()

    if result.truncated:
        print(f"{result.truncated} more file(s) left for the next run (max_files_per_run).")
    print("  ".join(f"{k}={v}" for k, v in sorted(result.counts.items())))
    if args.dry_run:
        print("\nDRY RUN — nothing was changed. Drop --dry-run to apply.")
    return 1 if result.by_status(FAILED) else 0


def seconds_to_next_hour(now: float) -> float:
    """Seconds from `now` until the next hour boundary in UTC-aligned epoch time.

    Computed from the clock rather than from when the last run finished, so a
    slow run does not push every later run later — the schedule cannot drift.
    """
    return 3600 - (now % 3600)


def cmd_loop(args: argparse.Namespace) -> int:
    """Run on the hour, forever.

    A plain sleep beats cron in a container: cron strips the environment, which
    is exactly where every secret lives.
    """
    import time

    while True:
        try:
            cmd_run(args)
        except (ConfigError, DriveError) as exc:
            # Config or Drive trouble must not kill the container; the next run
            # may well succeed, and a restart loop hides the cause.
            log.error("run failed: %s", exc)
        except Exception:
            log.exception("unexpected failure; continuing to the next hour")

        delay = seconds_to_next_hour(time.time())
        log.info("next run in %d minutes", round(delay / 60))
        time.sleep(delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drive-organiser", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-folders", help="print paste-ready TOML for visible folders").set_defaults(
        func=cmd_list_folders
    )
    sub.add_parser("check", help="validate credentials and config; changes nothing").set_defaults(
        func=cmd_check
    )
    for name, help_text, func in (
        ("run", "name and file each inbox file, once", cmd_run),
        ("loop", "run on the hour, forever (the container entrypoint)", cmd_loop),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="show what would happen and write nothing; for debugging",
        )
        p.add_argument("--no-email", action="store_true", help="print the summary instead of emailing it")
        p.set_defaults(func=func)
    return parser


# httplib2, which carries every Drive call, has no timeout of its own and
# inherits Python's default of None. Without this a stalled request blocks the
# hourly loop forever while the container still reports healthy — and the loop
# swallows exceptions, so nothing would ever restart it. This is a per-socket
# read timeout, not a total transfer budget, so slow large downloads still work.
SOCKET_TIMEOUT = 60


def main(argv: list[str] | None = None) -> int:
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    try:
        return args.func(args)
    except (ConfigError, DriveError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
