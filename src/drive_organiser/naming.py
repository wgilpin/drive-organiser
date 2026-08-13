"""Filename construction. Pure functions, no IO, no network.

Gemini proposes a stem; everything that makes it a safe, unique Drive filename
happens here. Kept separate so the awkward cases are cheap to test.
"""

from __future__ import annotations

import re
import unicodedata

# Illegal on Windows/macOS and confusing in Drive. Drive itself tolerates "/"
# in names but it breaks any later export to a filesystem.
ILLEGAL = r'\/:*?"<>|'
_ILLEGAL_RE = re.compile(f"[{re.escape(ILLEGAL)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")

MAX_STEM = 120
FALLBACK_STEM = "untitled"

# Extensions that carry a second dot and must not be split at the last one.
COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz")


def split_extension(name: str) -> tuple[str, str]:
    """Split 'report.v2.pdf' -> ('report.v2', '.pdf'). Returns '' if there is none."""
    lowered = name.lower()
    for suffix in COMPOUND:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], name[-len(suffix) :]

    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    # A long or non-alphanumeric tail is part of the name, not an extension:
    # "Notes on Q3. Final" must not become extension " Final".
    if len(ext) > 10 or not ext.isalnum():
        return name, ""
    return stem, f".{ext}"


def sanitise_stem(raw: str) -> str:
    """Strip anything unsafe or invisible. Returns '' if nothing usable remains."""
    text = unicodedata.normalize("NFC", raw or "")
    text = _CONTROL_RE.sub("", text)
    text = _ILLEGAL_RE.sub("-", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # Leading dots hide files; trailing dots and spaces are stripped by Windows.
    # Hyphens join the list because illegal characters were replaced by one above,
    # so "///" arrives here as "---".
    text = text.strip(". -")
    if len(text) > MAX_STEM:
        text = text[:MAX_STEM].rstrip(" .-")
    # A name made only of punctuation carries no information. Treat it as empty
    # so the caller falls back to the original name.
    if not any(char.isalnum() for char in text):
        return ""
    return text


def build_name(proposed_stem: str, original_name: str, *, is_native_google: bool) -> str:
    """Combine Gemini's proposed stem with the correct extension.

    Native Google files (Docs, Sheets, Slides) have no extension in their Drive
    name at all — the '.gdoc' suffix only exists in the desktop mount — so adding
    one would be wrong.
    """
    original_stem, original_ext = split_extension(original_name)

    stem = sanitise_stem(proposed_stem) or sanitise_stem(original_stem) or FALLBACK_STEM
    extension = "" if is_native_google else original_ext
    return f"{stem}{extension}"


def dedupe(name: str, existing: set[str]) -> str:
    """Append ' (2)', ' (3)' ... before the extension until the name is free."""
    if name not in existing:
        return name
    stem, extension = split_extension(name)
    counter = 2
    while True:
        candidate = f"{stem} ({counter}){extension}"
        if candidate not in existing:
            return candidate
        counter += 1
