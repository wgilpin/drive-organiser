"""The pipeline, against fakes. No network.

The first test is the one that matters most: a dry run must issue no mutating
Drive call whatsoever.
"""

import pytest
from fakes import FakeDrive, FakeNamer, make_file, make_folder

from drive_organiser.config import parse_config
from drive_organiser.organiser import DRY_RUN, FAILED, MOVED, SKIPPED, organise

CONFIG = """
[drive]
inbox_folder_id    = "inbox1"
unsorted_folder_id = "unsorted1"

[[destinations]]
id = "acad"
name = "Academic"
description = "papers"

[[destinations]]
id = "inv"
name = "Invoices"
description = "bills"

[gemini]
min_confidence = 0.6

[limits]
max_files_per_run = 3
"""


@pytest.fixture
def cfg():
    return parse_config(CONFIG)


class TestDryRun:
    def test_writing_is_the_default(self, cfg):
        """No flag means it files. Dry run is opt-in, for debugging only."""
        drive = FakeDrive()
        result = organise(drive, FakeNamer(), cfg)
        assert drive.mutations, "a run with no arguments must actually file"
        assert result.by_status(MOVED)
        assert result.dry_run is False

    def test_a_dry_run_makes_no_mutating_call(self, cfg):
        drive = FakeDrive()
        result = organise(drive, FakeNamer(), cfg, dry_run=True)
        assert drive.mutations == []
        assert result.by_status(DRY_RUN)
        assert result.dry_run is True

    def test_a_dry_run_still_produces_the_full_proposal(self, cfg):
        result = organise(FakeDrive(), FakeNamer(), cfg, dry_run=True)
        outcome = result.outcomes[0]
        assert outcome.new_name == "Proposed Name.pdf"
        assert outcome.dest_name == "Academic"
        assert outcome.confidence == 0.9


class TestExecute:
    def test_a_real_run_renames_and_moves_once_per_file(self, cfg):
        drive = FakeDrive()
        result = organise(drive, FakeNamer(), cfg, dry_run=False)
        assert drive.mutations == [("f1", "Proposed Name.pdf", "acad")]
        assert result.by_status(MOVED)

    def test_the_original_name_survives_the_move(self, cfg):
        """After the move file.name is the new name, so it must be captured early."""
        result = organise(FakeDrive(), FakeNamer(), cfg, dry_run=False)
        outcome = result.outcomes[0]
        assert outcome.original_name == "scan001.pdf"
        assert outcome.new_name == "Proposed Name.pdf"

    def test_the_link_still_resolves_after_the_move(self, cfg):
        result = organise(FakeDrive(), FakeNamer(), cfg, dry_run=False)
        assert result.outcomes[0].link.endswith("/f1/view")


class TestRouting:
    def test_low_confidence_is_filed_to_unsorted(self, cfg):
        drive = FakeDrive()
        organise(drive, FakeNamer(confidence=0.2), cfg, dry_run=False)
        assert drive.mutations[0][2] == "unsorted1"

    def test_an_unknown_folder_id_is_filed_to_unsorted(self, cfg):
        drive = FakeDrive()
        organise(drive, FakeNamer(folder_id="not-a-real-folder"), cfg, dry_run=False)
        assert drive.mutations[0][2] == "unsorted1"

    def test_unsorted_files_are_flagged_for_the_report(self, cfg):
        result = organise(FakeDrive(), FakeNamer(confidence=0.1), cfg, dry_run=True)
        assert result.outcomes[0].unsorted is True


class TestIsolation:
    def test_one_failing_file_does_not_abort_the_run(self, cfg):
        drive = FakeDrive(children=[make_file("f1"), make_file("f2", "b.pdf"), make_file("f3", "c.pdf")])

        class Flaky(FakeNamer):
            def propose(self, file, payload, destinations, min_confidence):
                if file.id == "f2":
                    raise RuntimeError("model exploded")
                return super().propose(file, payload, destinations, min_confidence)

        result = organise(drive, Flaky(), cfg, dry_run=False)
        assert len(result.by_status(MOVED)) == 2
        assert len(result.by_status(FAILED)) == 1
        assert result.by_status(FAILED)[0].error == "model exploded"

    def test_a_failed_file_is_not_moved(self, cfg):
        drive = FakeDrive()
        result = organise(drive, FakeNamer(raises=RuntimeError("boom")), cfg, dry_run=False)
        assert drive.mutations == []
        assert result.by_status(FAILED)


class TestSkipping:
    def test_files_the_account_cannot_rename_are_skipped(self, cfg):
        drive = FakeDrive(children=[make_file(caps={"canRename": False, "canMoveItemWithinDrive": True})])
        namer = FakeNamer()
        result = organise(drive, namer, cfg, dry_run=False)
        assert result.by_status(SKIPPED)
        assert drive.mutations == []
        assert namer.calls == 0, "must not spend a Gemini call on a file it cannot move"

    def test_folders_and_shortcuts_in_the_inbox_are_ignored(self, cfg):
        drive = FakeDrive(
            children=[
                make_file("f1"),
                make_file("d1", "a folder", "application/vnd.google-apps.folder"),
                make_file("s1", "a shortcut", "application/vnd.google-apps.shortcut"),
            ]
        )
        result = organise(drive, FakeNamer(), cfg, dry_run=True)
        assert len(result.outcomes) == 1


class TestNestedFolders:
    """Files inside subfolders of the inbox are processed; the folders are not."""

    def nested(self):
        return FakeDrive(
            children=[
                make_file("f1", "loose.pdf", parents=("inbox1",)),
                make_folder("sub1", "Scans", parent="inbox1"),
                make_file("f2", "nested.pdf", parents=("sub1",)),
                make_folder("sub2", "2024", parent="sub1"),
                make_file("f3", "deep.pdf", parents=("sub2",)),
            ]
        )

    def test_files_in_subfolders_are_processed(self):
        result = organise(self.nested(), FakeNamer(), parse_config(CONFIG), dry_run=True)
        assert {o.original_name for o in result.outcomes} == {"loose.pdf", "nested.pdf", "deep.pdf"}

    def test_the_subfolders_themselves_are_never_moved(self):
        drive = self.nested()
        organise(drive, FakeNamer(), parse_config(CONFIG), dry_run=False)
        moved_ids = {m[0] for m in drive.mutations}
        assert "sub1" not in moved_ids and "sub2" not in moved_ids

    def test_a_nested_file_is_detached_from_its_subfolder_not_the_inbox(self):
        """removeParents must be the file's own parent. Detaching from the inbox
        would leave the file in the subfolder and the move would silently fail."""
        drive = self.nested()
        organise(drive, FakeNamer(), parse_config(CONFIG), dry_run=False)
        detached = dict(drive.detached)
        assert detached["f2"] == ("sub1",)
        assert detached["f3"] == ("sub2",)
        assert detached["f1"] == ("inbox1",)

    def test_the_source_folder_is_recorded_for_the_report(self):
        result = organise(self.nested(), FakeNamer(), parse_config(CONFIG), dry_run=True)
        by_name = {o.original_name: o.source_folder for o in result.outcomes}
        assert by_name == {"loose.pdf": "", "nested.pdf": "Scans", "deep.pdf": "Scans/2024"}

    def test_depth_is_bounded(self):
        """A pathological tree must not walk forever."""
        children = [make_folder("d0", "d0", parent="inbox1")]
        for i in range(1, 9):
            children.append(make_folder(f"d{i}", f"d{i}", parent=f"d{i - 1}"))
        children.append(make_file("deep", "buried.pdf", parents=("d8",)))
        result = organise(FakeDrive(children=children), FakeNamer(), parse_config(CONFIG), dry_run=True)
        assert result.outcomes == []


class TestCollisions:
    def test_two_files_in_one_run_cannot_take_the_same_name(self, cfg):
        drive = FakeDrive(children=[make_file("f1", "a.pdf"), make_file("f2", "b.pdf")])
        organise(drive, FakeNamer(), cfg, dry_run=False)
        assert [m[1] for m in drive.mutations] == ["Proposed Name.pdf", "Proposed Name (2).pdf"]

    def test_a_name_already_in_the_destination_is_avoided(self, cfg):
        existing = make_file("x1", "Proposed Name.pdf", parents=("acad",))
        drive = FakeDrive(children=[make_file("f1"), existing])
        organise(drive, FakeNamer(), cfg, dry_run=False)
        assert drive.mutations[0][1] == "Proposed Name (2).pdf"


class TestLimits:
    def test_the_run_stops_at_max_files_per_run(self, cfg):
        drive = FakeDrive(children=[make_file(f"f{i}", f"{i}.pdf") for i in range(5)])
        result = organise(drive, FakeNamer(), cfg, dry_run=False)
        assert len(drive.mutations) == 3
        assert result.truncated == 2

    def test_an_empty_inbox_produces_no_outcomes(self, cfg):
        result = organise(FakeDrive(children=[]), FakeNamer(), cfg, dry_run=True)
        assert result.outcomes == []
        assert result.counts == {}
