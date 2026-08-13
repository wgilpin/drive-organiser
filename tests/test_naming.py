"""Filename construction. Pure, so the awkward cases are cheap to cover."""

import pytest

from drive_organiser.naming import MAX_STEM, build_name, dedupe, sanitise_stem, split_extension


class TestSplitExtension:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("report.pdf", ("report", ".pdf")),
            ("report.v2.pdf", ("report.v2", ".pdf")),
            ("archive.tar.gz", ("archive", ".tar.gz")),
            ("Annual Report", ("Annual Report", "")),
            (".hidden", (".hidden", "")),
            ("Notes on Q3. Final", ("Notes on Q3. Final", "")),
            ("data.superlongextension", ("data.superlongextension", "")),
        ],
    )
    def test_split(self, name, expected):
        assert split_extension(name) == expected


class TestSanitiseStem:
    @pytest.mark.parametrize("char", list(r'\/:*?"<>|'))
    def test_replaces_every_illegal_character(self, char):
        assert char not in sanitise_stem(f"a{char}b")

    def test_strips_control_characters(self):
        assert sanitise_stem("in\x00valid\x1fname") == "invalidname"

    def test_collapses_whitespace(self):
        assert sanitise_stem("too   many \n spaces") == "too many spaces"

    def test_strips_leading_and_trailing_dots_and_spaces(self):
        assert sanitise_stem("  ..name..  ") == "name"

    def test_truncates_to_the_cap(self):
        assert len(sanitise_stem("x" * 500)) == MAX_STEM

    def test_truncation_does_not_leave_trailing_punctuation(self):
        result = sanitise_stem("y" * (MAX_STEM - 1) + " - trailing")
        assert not result.endswith((" ", ".", "-"))

    @pytest.mark.parametrize("raw", ["", "   ", "...", "///", "\x00"])
    def test_returns_empty_when_nothing_survives(self, raw):
        assert sanitise_stem(raw) == ""

    def test_keeps_unicode(self):
        assert sanitise_stem("Café Ordnung 2024") == "Café Ordnung 2024"


class TestBuildName:
    def test_keeps_the_original_extension_for_binary_files(self):
        assert build_name("Pension Statement", "scan001.pdf", is_native_google=False) == (
            "Pension Statement.pdf"
        )

    def test_adds_no_extension_for_native_google_files(self):
        """A Doc's Drive name has no extension; '.gdoc' only exists in the mount."""
        assert build_name("Q3 Plan", "Untitled document", is_native_google=True) == "Q3 Plan"

    def test_native_google_file_does_not_inherit_a_dotted_tail(self):
        assert build_name("Budget", "Old Budget.v2", is_native_google=True) == "Budget"

    def test_sanitises_the_proposed_stem(self):
        assert build_name("Invoice 03/2026", "x.pdf", is_native_google=False) == "Invoice 03-2026.pdf"

    def test_falls_back_to_the_original_stem_when_the_proposal_is_empty(self):
        assert build_name("   ", "Original Name.pdf", is_native_google=False) == "Original Name.pdf"

    def test_falls_back_to_untitled_when_both_are_unusable(self):
        assert build_name("///", "...", is_native_google=False) == "untitled"

    def test_compound_extension_is_preserved(self):
        assert build_name("Backup", "stuff.tar.gz", is_native_google=False) == "Backup.tar.gz"


class TestDedupe:
    def test_leaves_a_free_name_alone(self):
        assert dedupe("Report.pdf", {"Other.pdf"}) == "Report.pdf"

    def test_appends_a_counter_before_the_extension(self):
        assert dedupe("Report.pdf", {"Report.pdf"}) == "Report (2).pdf"

    def test_keeps_counting_past_existing_duplicates(self):
        existing = {"Report.pdf", "Report (2).pdf", "Report (3).pdf"}
        assert dedupe("Report.pdf", existing) == "Report (4).pdf"

    def test_works_without_an_extension(self):
        assert dedupe("Q3 Plan", {"Q3 Plan"}) == "Q3 Plan (2)"
