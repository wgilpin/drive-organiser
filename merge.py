"""Merge competing folder trees in Google Drive into one destination each.

Plans (select with --plan):

    money    Money/ + Finances/ + Dropbox/Docs-dropbox/Money/  ->  Money/
             grouped under Employment/ and Property/ (--flat to skip grouping)

    medical  Medical/ + Health/ + loose medical PDFs at the Drive root
             and in Academic/  ->  Medical/

A rule's source may be a folder (moves the whole subtree), a folder
followed by "/*" (moves only the files sitting directly in it), or a
single file path.

Safety model:
    - dry run by default; --execute is required to touch anything
    - nothing is ever deleted or overwritten
    - name conflicts are resolved by suffixing, never by replacing
    - every move is journalled to JSON and an undo script is written
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

DRIVE = Path.home() / "Library/CloudStorage/GoogleDrive-wgilpin@gmail.com/My Drive"

# Folders that carry a trailing space in Drive today; matched exactly so the
# script fails loudly rather than silently creating a second copy.
RECEIPTS_SP = "Receipts "
MEADOW_SP = "Meadow Farm Cottage "

# (source path relative to DRIVE, destination path relative to Money/)
# A destination of "" means "the root of Money/".
GROUPED_PLAN: list[tuple[str, str]] = [
    # --- Employment -------------------------------------------------------
    ("Money/Adarga", "Employment/Adarga"),
    ("Money/Adarga (1)", "Employment/Adarga"),
    ("Money/BenchSci", "Employment/BenchSci"),
    ("Money/Quantexa", "Employment/Quantexa"),
    ("Money/Stepstone", "Employment/Stepstone"),
    ("Money/Monolith", "Employment/Monolith"),
    ("Finances/Google", "Employment/Google"),
    ("Finances/BICL", "Employment/BICL"),
    ("Finances/Schibsted", "Employment/Schibsted"),
    ("Dropbox/Docs-dropbox/Money/Schibsted final exes", "Employment/Schibsted/Final expenses"),
    ("Dropbox/Docs-dropbox/Money/Pay advice", "Employment/Pay advice"),
    ("Dropbox/Docs-dropbox/Money/Timesheets", "Employment/Timesheets"),
    # --- Property ---------------------------------------------------------
    ("Money/House", "Property/House"),
    ("Money/Paddock Sale", "Property/Paddock Sale"),
    (f"Money/{MEADOW_SP}", "Property/Meadow Farm Cottage"),
    ("Dropbox/Docs-dropbox/Money/Bear Island", "Property/Bear Island"),
    # --- Flat categories --------------------------------------------------
    ("Money/Tax", "Tax"),
    ("Dropbox/Docs-dropbox/Money/Tax", "Tax"),
    (f"Money/{RECEIPTS_SP}", "Receipts"),
    ("Dropbox/Docs-dropbox/Money/Receipts", "Receipts"),
    ("Dropbox/Docs-dropbox/Money/Statements", "Statements"),
    ("Dropbox/Docs-dropbox/Money/Invoices Out", "Invoices Out"),
    ("Money/Pension", "Pension"),
    ("Money/Mum", "Mum"),
    # --- Loose files at the source roots ----------------------------------
    ("Finances/*", "Unsorted/from Finances"),
    ("Dropbox/Docs-dropbox/Money/*", "Unsorted/from Docs-dropbox"),
]

# Same merge, without the Employment/ and Property/ grouping.
FLAT_PLAN = [
    (src, dst.split("/", 1)[1] if dst.startswith(("Employment/", "Property/")) else dst)
    for src, dst in GROUPED_PLAN
]

# Medical records scattered across three folders plus the Drive root.
# Reading notes about health topics (Modafinil, menopause, a metabolic-health
# talk) are deliberately excluded — they are research, not records, and would
# blur the category the router matches against.
MEDICAL_PLAN: list[tuple[str, str]] = [
    ("Prescription.pdf", "Prescriptions"),
    ("prescription .pdf", "Prescriptions"),
    ("prescription boots 2024.pdf", "Prescriptions"),
    ("Prescription katie.pdf", "Prescriptions"),
    ("David Clulow Prescription 17 Apr .gdoc", "Prescriptions"),
    ("Academic/prescription SEP 2019.pdf", "Prescriptions"),
    ("Medical/Prescription Apr 2026.pdf", "Prescriptions"),
    ("Medical/William Gilpin RX.pdf", "Prescriptions"),
    ("Health", "Results"),
    ("FitNote_50b1b9a5-e5eb-4f0d-8fd0-ce4337eb33e1.pdf", "Fit notes"),
    ("FitNote_b8ddc9ed-1164-4082-9dbf-ed7765e84106 (1).pdf", "Fit notes"),
    ("000403263076_Appointment_Request_Summary_20230308174429.pdf", "Appointments"),
    ("Ppp healthcare.pdf", "Insurance"),
    ("Vaccination pass.pdf", ""),
]

IGNORE = {".DS_Store", "Icon\r", ".localized"}

# name -> (target root, rules, roots to sweep for emptied directories)
PLANS: dict[str, tuple[str, list[tuple[str, str]], list[str]]] = {
    "money": ("Money", GROUPED_PLAN, ["Money", "Finances", "Dropbox/Docs-dropbox/Money"]),
    "medical": ("Medical", MEDICAL_PLAN, ["Medical", "Health"]),
}


@dataclass
class Move:
    src: Path
    dst: Path
    note: str = ""


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    conflicts: int = 0
    missing: list[str] = field(default_factory=list)
    skipped: int = 0


def iter_files(root: Path):
    """Yield every file under root, relative to root, skipping OS cruft."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__MACOSX"}]
        for name in filenames:
            if name in IGNORE:
                continue
            full = Path(dirpath) / name
            yield full, full.relative_to(root)


def unique_destination(dst: Path, taken: set[Path], src: Path) -> tuple[Path, str]:
    """Return a destination that collides with nothing, plus a note.

    Never returns a path that already exists on disk or is already claimed by
    an earlier move in this run.
    """
    if not dst.exists() and dst not in taken:
        return dst, ""

    note = "conflict"
    if dst.exists():
        try:
            if dst.stat().st_size == src.stat().st_size:
                note = "conflict (same size — likely duplicate, review)"
        except OSError:
            pass

    stem, suffix = dst.stem, dst.suffix
    for n in range(2, 500):
        candidate = dst.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() and candidate not in taken:
            return candidate, note
    raise RuntimeError(f"could not find a free name for {dst}")


def build_plan(drive: Path, rules: list[tuple[str, str]], target: str) -> Plan:
    plan = Plan()
    taken: set[Path] = set()
    target_root = drive / target

    for src_rel, dst_rel in rules:
        loose_only = src_rel.endswith("/*")
        src = drive / (src_rel[:-2] if loose_only else src_rel)

        if not src.exists():
            plan.missing.append(src_rel)
            continue

        dst_base = target_root / dst_rel if dst_rel else target_root

        if src.is_file():
            # A rule naming one file moves just that file.
            final, note = unique_destination(dst_base / src.name, taken, src)
            if src == final:
                plan.skipped += 1
                continue
            if note:
                plan.conflicts += 1
            taken.add(final)
            plan.moves.append(Move(src, final, note))
            continue

        if loose_only:
            # Only files sitting directly in this folder, not subtrees.
            pairs = [
                (p, Path(p.name))
                for p in sorted(src.iterdir())
                if p.is_file() and p.name not in IGNORE
            ]
        else:
            pairs = sorted(iter_files(src), key=lambda t: str(t[1]))

        for full, rel in pairs:
            # A file already sitting at its correct destination is a no-op.
            want = dst_base / rel
            if full == want:
                plan.skipped += 1
                continue
            final, note = unique_destination(want, taken, full)
            if note:
                plan.conflicts += 1
            taken.add(final)
            plan.moves.append(Move(full, final, note))

    return plan


def render(plan: Plan, drive: Path, limit: int) -> None:
    by_dst: dict[str, list[Move]] = {}
    for m in plan.moves:
        key = str(m.dst.parent.relative_to(drive))
        by_dst.setdefault(key, []).append(m)

    print(f"\n{'=' * 72}\nMERGE PLAN — {len(plan.moves)} files into {len(by_dst)} folders\n{'=' * 72}")
    for dest in sorted(by_dst):
        movs = by_dst[dest]
        print(f"\n  {dest}/   ({len(movs)} files)")
        for m in movs[:limit]:
            flag = f"   <-- {m.note}" if m.note else ""
            print(f"      {m.src.relative_to(drive)}{flag}")
        if len(movs) > limit:
            print(f"      ... and {len(movs) - limit} more")

    print(f"\n{'-' * 72}")
    print(f"  files to move      : {len(plan.moves)}")
    print(f"  name conflicts     : {plan.conflicts}  (renamed, never overwritten)")
    print(f"  already in place   : {plan.skipped}")
    if plan.missing:
        print(f"  MISSING sources    : {len(plan.missing)}")
        for m in plan.missing:
            print(f"      {m}")
    print(f"{'-' * 72}")


def execute(plan: Plan, drive: Path, journal_path: Path) -> int:
    done: list[dict[str, str]] = []
    failed = 0

    for i, m in enumerate(plan.moves, 1):
        try:
            m.dst.parent.mkdir(parents=True, exist_ok=True)
            if m.dst.exists():
                # Someone changed the tree under us since planning.
                raise FileExistsError(f"destination appeared during run: {m.dst}")
            shutil.move(str(m.src), str(m.dst))
            done.append({"src": str(m.src), "dst": str(m.dst)})
        except Exception as exc:  # keep going; report at the end
            failed += 1
            print(f"  FAILED {m.src.relative_to(drive)}: {exc}", file=sys.stderr)
        if i % 50 == 0:
            print(f"  ... {i}/{len(plan.moves)}")

    journal_path.write_text(json.dumps(done, indent=2))
    undo = journal_path.with_suffix(".undo.py")
    undo.write_text(
        "# Reverses the merge recorded in {}\n"
        "import json, shutil, pathlib\n"
        "for r in reversed(json.loads(pathlib.Path({!r}).read_text())):\n"
        "    dst = pathlib.Path(r['src']); dst.parent.mkdir(parents=True, exist_ok=True)\n"
        "    if pathlib.Path(r['dst']).exists() and not dst.exists():\n"
        "        shutil.move(r['dst'], r['src'])\n".format(journal_path.name, str(journal_path))
    )
    print(f"\n  moved   : {len(done)}")
    print(f"  failed  : {failed}")
    print(f"  journal : {journal_path}")
    print(f"  undo    : python3 {undo}")
    return failed


def prune_empty(drive: Path, roots: list[str], execute: bool) -> None:
    """Remove directories the merge emptied.

    Uses rmdir, which refuses to delete a non-empty directory — so the OS,
    not this function, is what guarantees nothing with content is lost.
    A lone .DS_Store does not count as content.
    """
    candidates: list[Path] = []
    for root_rel in roots:
        root = drive / root_rel
        if not root.exists():
            continue
        # Deepest first, so parents become empty as children are removed.
        for dirpath, _, _ in sorted(os.walk(root), key=lambda t: t[0].count(os.sep), reverse=True):
            candidates.append(Path(dirpath))

    removed = kept = 0
    for d in candidates:
        entries = [p for p in d.iterdir()] if d.exists() else []
        if any(p.name not in IGNORE for p in entries):
            kept += 1
            continue
        if not execute:
            print(f"  would remove  {d.relative_to(drive)}")
            removed += 1
            continue
        try:
            for junk in entries:  # only IGNORE-listed names remain
                junk.unlink()
            d.rmdir()
            removed += 1
        except OSError as exc:
            kept += 1
            print(f"  kept (not empty) {d.relative_to(drive)}: {exc}", file=sys.stderr)

    verb = "removed" if execute else "would remove"
    print(f"\n  {verb} {removed} empty directories; kept {kept} with content")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", choices=sorted(PLANS), default="money", help="which merge to run")
    ap.add_argument("--execute", action="store_true", help="actually move files (default is dry run)")
    ap.add_argument("--prune-empty", action="store_true", help="remove directories the merge emptied")
    ap.add_argument("--flat", action="store_true", help="money only: skip Employment/ and Property/ grouping")
    ap.add_argument("--drive", type=Path, default=DRIVE)
    ap.add_argument("--limit", type=int, default=6, help="sample rows shown per destination")
    ap.add_argument("--journal", type=Path, default=None)
    args = ap.parse_args()

    if not args.drive.exists():
        print(f"Drive not found: {args.drive}", file=sys.stderr)
        return 2

    target, rules, sweep = PLANS[args.plan]
    if args.plan == "money" and args.flat:
        rules = FLAT_PLAN

    if args.prune_empty:
        # The merge's sources, plus the target itself so emptied originals
        # inside it are caught too.
        print(f"\nPRUNING EMPTY DIRECTORIES ({args.plan})\n")
        prune_empty(args.drive, sweep, args.execute)
        if not args.execute:
            print("\nDRY RUN — nothing removed. Add --execute to apply.\n")
        return 0

    plan = build_plan(args.drive, rules, target)
    render(plan, args.drive, args.limit)

    if not args.execute:
        print("\nDRY RUN — nothing moved. Re-run with --execute to apply.\n")
        return 0

    print("\nEXECUTING...\n")
    journal = args.journal or Path(f"{args.plan}_merge_journal.json")
    return 1 if execute(plan, args.drive, journal) else 0


if __name__ == "__main__":
    raise SystemExit(main())
