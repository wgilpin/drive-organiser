"""The only module that touches the Gemini SDK.

Call shape verified against the installed google-genai 2.16.0:
  client.models.generate_content(model=..., contents=[...],
      config=types.GenerateContentConfig(
          response_mime_type="application/json",
          response_schema=<pydantic class>))
  -> response.parsed is the model instance, or None if parsing failed.

`client.interactions` exists but is the Agent Platform trigger surface, not a
replacement for generate_content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .config import Destination
from .drive import DriveFile
from .extract import Payload

log = logging.getLogger(__name__)

UNSORTED = "__unsorted__"

INSTRUCTIONS = """\
You name and file documents for a personal Google Drive.

Given one file, do two things:
1. Propose a short, descriptive filename stem. No extension. Title Case.
   Describe what the document *is* — issuer, then subject. Never invent facts
   that are not in the document.
2. Choose exactly one destination folder id from the list below.

Filename format — follow exactly:
- Pattern: "<Issuer> <Subject> <YYYY-MM-DD>", date last, always.
- Write the date only as YYYY-MM-DD. Never "20 May 2018", "04 Jul 2026" or
  "May 2025". Never put the date first or in the middle.
- Use the date the document was issued or dated. If only a month is legible,
  use the first day of it. If no date is legible, leave the date off entirely
  rather than guess.
- Good: "John Lewis Dyson Fan Receipt 2018-05-20"
- Bad:  "2026-03-29 Rodd and Gunn Purchase Receipt", "Receipt 04 Jul 2026"

Rules:
- Return a folder id copied exactly from the list. Never invent an id.
- If no folder is a clear fit, or the content is too thin to judge, return
  "{unsorted}" and a confidence below 0.5.
- confidence is your honest probability that the folder is correct.
- reason is one short sentence for a human reading a summary email.

Destination folders:
{folders}
"""


class FileDecision(BaseModel):
    filename: str = Field(description="Proposed filename stem, no extension.")
    folder_id: str = Field(description="Exactly one id from the destination list.")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(description="One short sentence.")


@dataclass(frozen=True)
class Proposal:
    decision: FileDecision
    routed_to_unsorted: bool
    routing_note: str


def build_prompt(destinations: tuple[Destination, ...]) -> str:
    lines = [f'  - id: "{d.id}"\n    name: {d.name}\n    holds: {d.description}' for d in destinations]
    lines.append(f'  - id: "{UNSORTED}"\n    name: _Unsorted\n    holds: anything that fits nothing above.')
    return INSTRUCTIONS.format(unsorted=UNSORTED, folders="\n".join(lines))


def describe(file: DriveFile, payload: Payload) -> str:
    parts = [
        "File metadata:",
        f"  current name: {file.name}",
        f"  type: {file.mime_type}",
        f"  size: {file.size if file.size is not None else 'unknown'}",
        f"  last modified: {file.modified_time}",
    ]
    if payload.kind == "metadata":
        parts.append(f"\nContent unavailable ({payload.note}). Judge from the metadata alone.")
    elif payload.kind == "text":
        parts.append(f"\nContent:\n{payload.text}")
    return "\n".join(parts)


class GeminiNamer:
    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def propose(
        self, file: DriveFile, payload: Payload, destinations: tuple[Destination, ...], min_confidence: float
    ) -> Proposal:
        contents: list = [build_prompt(destinations), describe(file, payload)]
        if payload.kind == "bytes" and payload.data:
            contents.append(types.Part.from_bytes(data=payload.data, mime_type=payload.mime_type or ""))

        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FileDecision,
            ),
        )

        decision = response.parsed
        if not isinstance(decision, FileDecision):
            # parsed is typed BaseModel | dict | Enum | None, so None is possible.
            log.warning("Gemini returned no parsable decision for %s", file.name)
            return Proposal(
                decision=FileDecision(
                    filename=file.name, folder_id=UNSORTED, confidence=0.0, reason="model returned no decision"
                ),
                routed_to_unsorted=True,
                routing_note="unparsable response",
            )

        return self._route(decision, destinations, min_confidence)

    @staticmethod
    def _route(
        decision: FileDecision, destinations: tuple[Destination, ...], min_confidence: float
    ) -> Proposal:
        """Enforce the allowlist and the confidence floor in code, not in the prompt.

        This is also the containment for prompt injection: a document telling the
        model to file itself somewhere else can only ever name a configured folder.
        """
        allowed = {d.id for d in destinations}

        if decision.folder_id == UNSORTED:
            return Proposal(decision, True, "model chose _Unsorted")
        if decision.folder_id not in allowed:
            return Proposal(decision, True, f"model returned unknown folder id {decision.folder_id!r}")
        if decision.confidence < min_confidence:
            return Proposal(decision, True, f"confidence {decision.confidence:.2f} below {min_confidence:.2f}")
        return Proposal(decision, False, "")
