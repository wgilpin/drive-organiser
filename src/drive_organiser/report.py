"""Render a run as an email. Pure functions — no IO, no network.

Filenames come from documents and from a language model, so every one of them
is escaped before it reaches the HTML. A file named "<script>" must not break
the message.
"""

from __future__ import annotations

from html import escape

from .organiser import DRY_RUN, FAILED, MOVED, SKIPPED, FileOutcome, RunResult

# Inline styles only: Gmail and Outlook strip <style> blocks.
TABLE = "width:100%;border-collapse:collapse;font-size:14px;margin:0 0 24px"
TH = "text-align:left;padding:6px 8px;border-bottom:2px solid #ccc;font-weight:600"
TD = "padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top"
MUTED = "color:#666;font-size:13px"

SECTIONS = [
    (MOVED, "Filed", "#1a7f37"),
    (DRY_RUN, "Proposed (dry run — nothing was changed)", "#9a6700"),
    (SKIPPED, "Skipped", "#666666"),
    (FAILED, "Failed", "#cf222e"),
]


def build_subject(result: RunResult) -> str:
    counts = result.counts
    parts: list[str] = []
    if counts.get(MOVED):
        parts.append(f"{counts[MOVED]} filed")
    if counts.get(DRY_RUN):
        parts.append(f"{counts[DRY_RUN]} proposed")

    unsorted = sum(1 for o in result.outcomes if o.unsorted)
    if unsorted:
        parts.append(f"{unsorted} unsorted")
    if counts.get(SKIPPED):
        parts.append(f"{counts[SKIPPED]} skipped")
    if counts.get(FAILED):
        parts.append(f"{counts[FAILED]} FAILED")

    summary = ", ".join(parts) if parts else "nothing to do"
    prefix = "[DRY RUN] " if result.dry_run else ""
    return f"{prefix}Drive Organiser: {summary}"


def should_send(result: RunResult, *, email_on_empty: bool) -> bool:
    """An hourly job that mails on every empty run becomes noise."""
    if result.by_status(FAILED):
        return True  # failures are always worth an email
    if not result.outcomes:
        return email_on_empty
    return True


def _link(outcome: FileOutcome) -> str:
    name = escape(outcome.new_name or outcome.original_name)
    if not outcome.link:
        return name
    return f'<a href="{escape(outcome.link, quote=True)}" style="color:#0969da">{name}</a>'


def _row(outcome: FileOutcome) -> str:
    source = f" <span style='{MUTED}'>(from {escape(outcome.source_folder)}/)</span>" if outcome.source_folder else ""
    detail = outcome.error or outcome.note or ""

    if outcome.status in (MOVED, DRY_RUN):
        confidence = f"{outcome.confidence:.2f}"
        folder = escape(outcome.dest_name)
        if outcome.unsorted:
            folder = f"<strong>{folder}</strong>"
        second = f"{folder} <span style='{MUTED}'>· {confidence}</span>"
    else:
        second = f"<span style='{MUTED}'>{escape(detail)}</span>"
        detail = ""

    reason = f"<div style='{MUTED}'>{escape(outcome.reason or detail)}</div>" if (outcome.reason or detail) else ""

    return (
        f"<tr>"
        f"<td style='{TD}'><span style='{MUTED}'>{escape(outcome.original_name)}</span>{source}"
        f"<div>{_link(outcome)}</div>{reason}</td>"
        f"<td style='{TD};white-space:nowrap'>{second}</td>"
        f"</tr>"
    )


def render_html(result: RunResult) -> str:
    blocks: list[str] = []
    for status, title, colour in SECTIONS:
        rows = result.by_status(status)
        if not rows:
            continue
        blocks.append(
            f"<h2 style='font-size:15px;color:{colour};margin:20px 0 8px'>{title} ({len(rows)})</h2>"
            f"<table style='{TABLE}'>"
            f"<tr><th style='{TH}'>File</th><th style='{TH}'>Folder</th></tr>"
            + "".join(_row(o) for o in rows)
            + "</table>"
        )

    if not blocks:
        blocks.append(f"<p style='{MUTED}'>The inbox was empty.</p>")

    footer = []
    if result.truncated:
        footer.append(f"{result.truncated} file(s) left for the next run (max_files_per_run).")
    if result.dry_run:
        footer.append("This was a dry run. Nothing in Drive was changed.")
    if any(o.unsorted for o in result.outcomes):
        footer.append("Files in _Unsorted need filing by hand, or a new folder in config.toml.")

    footer_html = (
        f"<p style='{MUTED};border-top:1px solid #eee;padding-top:12px'>" + "<br>".join(footer) + "</p>"
        if footer
        else ""
    )
    return (
        "<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "max-width:760px;margin:0 auto;color:#1f2328'>"
        f"<h1 style='font-size:18px;margin:0 0 4px'>{escape(build_subject(result))}</h1>"
        + "".join(blocks)
        + footer_html
        + "</div>"
    )


def render_text(result: RunResult) -> str:
    """Plain-text alternative, for clients that refuse HTML."""
    lines = [build_subject(result), ""]
    for status, title, _ in SECTIONS:
        rows = result.by_status(status)
        if not rows:
            continue
        lines.append(f"{title} ({len(rows)})")
        for o in rows:
            where = f" [from {o.source_folder}/]" if o.source_folder else ""
            lines.append(f"  {o.original_name}{where}")
            if o.status in (MOVED, DRY_RUN):
                lines.append(f"      -> {o.new_name}  ({o.dest_name}, {o.confidence:.2f})")
                if o.reason:
                    lines.append(f"         {o.reason}")
            else:
                lines.append(f"      {o.error or o.note}")
            if o.link:
                lines.append(f"         {o.link}")
        lines.append("")
    if result.dry_run:
        lines.append("DRY RUN — nothing in Drive was changed.")
    return "\n".join(lines)
