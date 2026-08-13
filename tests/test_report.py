"""Email rendering. Pure, so every branch is covered offline."""

from fakes import make_file

from drive_organiser.organiser import DRY_RUN, FAILED, MOVED, SKIPPED, FileOutcome, RunResult
from drive_organiser.report import build_subject, render_html, render_text, should_send


def outcome(status=MOVED, **kw):
    defaults = dict(
        file=make_file(),
        status=status,
        new_name="Nice Name 2026-01-01.pdf",
        dest_name="Academic",
        reason="because it is a paper",
        confidence=0.95,
    )
    return FileOutcome(**{**defaults, **kw})


def result(outcomes, dry_run=False, truncated=0):
    return RunResult(outcomes=outcomes, dry_run=dry_run, truncated=truncated)


class TestSubject:
    def test_counts_filed_files(self):
        assert build_subject(result([outcome(), outcome()])) == "Drive Organiser: 2 filed"

    def test_a_dry_run_is_marked_and_counted_separately(self):
        subject = build_subject(result([outcome(DRY_RUN)], dry_run=True))
        assert subject.startswith("[DRY RUN] ")
        assert "1 proposed" in subject

    def test_unsorted_files_are_called_out(self):
        assert "1 unsorted" in build_subject(result([outcome(unsorted=True)]))

    def test_failures_are_shouted(self):
        assert "1 FAILED" in build_subject(result([outcome(FAILED, error="boom")]))

    def test_skips_are_counted(self):
        assert "1 skipped" in build_subject(result([outcome(SKIPPED, note="no permission")]))

    def test_an_empty_run_says_so(self):
        assert build_subject(result([])) == "Drive Organiser: nothing to do"


class TestShouldSend:
    def test_an_empty_run_is_silent_by_default(self):
        """Hourly mail about an empty inbox is noise."""
        assert should_send(result([]), email_on_empty=False) is False

    def test_an_empty_run_can_be_opted_into(self):
        assert should_send(result([]), email_on_empty=True) is True

    def test_a_run_with_work_always_sends(self):
        assert should_send(result([outcome()]), email_on_empty=False) is True

    def test_failures_send_even_when_empty_mail_is_off(self):
        failed = result([outcome(FAILED, error="boom")])
        assert should_send(failed, email_on_empty=False) is True


class TestHtml:
    def test_hostile_filenames_are_escaped(self):
        """A document named like markup must not break the message."""
        html = render_html(result([outcome(file=make_file(name="<script>alert(1)</script>.pdf"))]))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_the_link_is_an_anchor(self):
        html = render_html(result([outcome()]))
        assert 'href="https://drive.google.com/file/d/f1/view"' in html

    def test_a_quote_in_a_link_cannot_escape_the_attribute(self):
        hostile = make_file(name="x.pdf")
        hostile = type(hostile)(**{**hostile.__dict__, "web_view_link": 'https://x/"onload="alert(1)'})
        html = render_html(result([outcome(file=hostile)]))
        assert 'onload="alert(1)' not in html

    def test_each_status_gets_its_own_section(self):
        html = render_html(
            result(
                [
                    outcome(MOVED),
                    outcome(SKIPPED, note="no permission"),
                    outcome(FAILED, error="boom"),
                ]
            )
        )
        for heading in ("Filed", "Skipped", "Failed"):
            assert heading in html

    def test_empty_sections_are_omitted(self):
        assert "Skipped" not in render_html(result([outcome(MOVED)]))

    def test_an_empty_run_still_renders(self):
        assert "The inbox was empty." in render_html(result([]))

    def test_a_dry_run_says_nothing_changed(self):
        assert "Nothing in Drive was changed" in render_html(result([outcome(DRY_RUN)], dry_run=True))

    def test_truncation_is_reported(self):
        assert "12 file(s) left" in render_html(result([outcome()], truncated=12))

    def test_unsorted_prompts_an_action(self):
        assert "need filing by hand" in render_html(result([outcome(unsorted=True)]))

    def test_the_source_subfolder_is_shown(self):
        assert "from Scans/" in render_html(result([outcome(source_folder="Scans")]))


class TestText:
    def test_the_plain_alternative_carries_the_essentials(self):
        text = render_text(result([outcome()]))
        assert "scan001.pdf" in text
        assert "Nice Name 2026-01-01.pdf" in text
        assert "Academic" in text

    def test_the_plain_alternative_marks_a_dry_run(self):
        assert "DRY RUN" in render_text(result([outcome(DRY_RUN)], dry_run=True))
