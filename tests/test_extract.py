"""The mimeType routing table, plus the confidence and allowlist gates."""

import pytest

from drive_organiser.config import Destination
from drive_organiser.extract import plan_extraction
from drive_organiser.gemini import UNSORTED, FileDecision, GeminiNamer, build_prompt

BIG = 20_000_000

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class TestPlanExtraction:
    @pytest.mark.parametrize(
        "mime,export_mime,kind",
        [(DOC, "text/plain", "text"), (SHEET, "text/csv", "text"), (SLIDES, "application/pdf", "bytes")],
    )
    def test_native_google_types_are_exported(self, mime, export_mime, kind):
        plan = plan_extraction(mime, 1000, BIG)
        assert (plan.action, plan.export_mime, plan.as_kind) == ("export", export_mime, kind)

    @pytest.mark.parametrize("mime", ["application/pdf", "image/png", "image/jpeg", DOCX])
    def test_binary_types_are_downloaded_as_bytes(self, mime):
        plan = plan_extraction(mime, 1000, BIG)
        assert (plan.action, plan.as_kind) == ("download", "bytes")

    @pytest.mark.parametrize("mime", ["text/plain", "text/markdown", "text/csv", "application/json"])
    def test_text_types_are_downloaded_as_text(self, mime):
        plan = plan_extraction(mime, 1000, BIG)
        assert (plan.action, plan.as_kind) == ("download", "text")

    @pytest.mark.parametrize(
        "mime", ["application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"]
    )
    def test_containers_are_skipped(self, mime):
        assert plan_extraction(mime, None, BIG).action == "skip"

    @pytest.mark.parametrize("mime", ["application/vnd.google-apps.form", "application/vnd.google-apps.site"])
    def test_other_google_types_fall_back_to_metadata(self, mime):
        plan = plan_extraction(mime, None, BIG)
        assert plan.action == "metadata"
        assert "no text export" in plan.reason

    def test_unknown_types_fall_back_to_metadata(self):
        plan = plan_extraction("application/x-nintendo-rom", 10, BIG)
        assert plan.action == "metadata"
        assert "unsupported" in plan.reason

    def test_oversized_files_fall_back_to_metadata(self):
        plan = plan_extraction("application/pdf", BIG + 1, BIG)
        assert plan.action == "metadata"
        assert "exceeds the inline limit" in plan.reason

    def test_the_size_guard_does_not_apply_to_native_google_files(self):
        """Their `size` is not the exported size, so it must not gate the export."""
        assert plan_extraction(DOC, BIG + 1, BIG).action == "export"

    def test_unknown_size_is_not_treated_as_oversized(self):
        assert plan_extraction("application/pdf", None, BIG).action == "download"


class TestRouting:
    DESTS = (
        Destination(id="acad", name="Academic", description="papers"),
        Destination(id="inv", name="Invoices", description="bills"),
    )

    def route(self, folder_id, confidence, minimum=0.6):
        decision = FileDecision(filename="X", folder_id=folder_id, confidence=confidence, reason="r")
        return GeminiNamer._route(decision, self.DESTS, minimum)

    def test_a_confident_known_folder_is_accepted(self):
        assert self.route("acad", 0.9).routed_to_unsorted is False

    def test_low_confidence_goes_to_unsorted(self):
        result = self.route("acad", 0.3)
        assert result.routed_to_unsorted is True
        assert "below" in result.routing_note

    def test_confidence_exactly_at_the_floor_is_accepted(self):
        assert self.route("acad", 0.6, 0.6).routed_to_unsorted is False

    def test_an_unknown_folder_id_goes_to_unsorted(self):
        """Containment for prompt injection: an invented id can never be used."""
        result = self.route("../../etc/passwd", 1.0)
        assert result.routed_to_unsorted is True
        assert "unknown folder id" in result.routing_note

    def test_the_models_own_unsorted_choice_is_honoured(self):
        assert self.route(UNSORTED, 0.9).routed_to_unsorted is True


class TestPrompt:
    def test_every_destination_reaches_the_prompt(self):
        prompt = build_prompt(TestRouting.DESTS)
        for d in TestRouting.DESTS:
            assert d.id in prompt
            assert d.name in prompt
            assert d.description in prompt

    def test_unsorted_is_always_offered(self):
        assert UNSORTED in build_prompt(TestRouting.DESTS)
