"""Reconciliation between the folders Drive shows and the folders config lists."""

from drive_organiser.cli import find_unconfigured, seconds_to_next_hour
from drive_organiser.config import parse_config
from drive_organiser.drive import FOLDER_MIME, DriveFile

ROOT = "root1"

CONFIG = """
[drive]
inbox_folder_id    = "inbox1"
unsorted_folder_id = "unsorted1"

[[destinations]]
id = "acad"
name = "Academic"
description = "papers"
"""


def folder(folder_id: str, name: str, parent: str = ROOT) -> DriveFile:
    return DriveFile(
        id=folder_id,
        name=name,
        mime_type=FOLDER_MIME,
        web_view_link="",
        parents=(parent,),
        capabilities={"canAddChildren": True},
        size=None,
        modified_time="",
    )


BASE = [
    folder(ROOT, "Organised", parent="mydrive"),
    folder("inbox1", "_Inbox"),
    folder("unsorted1", "_Unsorted"),
    folder("acad", "Academic"),
]


def test_reports_nothing_when_config_is_complete():
    assert find_unconfigured(BASE, parse_config(CONFIG)) == []


def test_reports_a_folder_missing_from_config():
    folders = [*BASE, folder("pens", "Pensions")]
    assert [f.name for f in find_unconfigured(folders, parse_config(CONFIG))] == ["Pensions"]


def test_results_are_sorted_by_name():
    folders = [*BASE, folder("z", "Zebra"), folder("a", "Apple")]
    assert [f.name for f in find_unconfigured(folders, parse_config(CONFIG))] == ["Apple", "Zebra"]


def test_the_shared_ancestor_is_never_reported():
    """Organised is the container. Filing into it would scatter files."""
    assert all(f.name != "Organised" for f in find_unconfigured(BASE, parse_config(CONFIG)))


def test_a_subfolder_of_a_destination_is_not_reported():
    """Academic/Papers is part of Academic, not a missed destination."""
    folders = [*BASE, folder("papers", "Papers", parent="acad")]
    assert find_unconfigured(folders, parse_config(CONFIG)) == []


def test_returns_nothing_when_the_inbox_is_not_visible():
    """Without the inbox there is no way to know which folder is the ancestor."""
    folders = [f for f in BASE if f.id != "inbox1"]
    assert find_unconfigured(folders, parse_config(CONFIG)) == []


class TestSchedule:
    """The delay comes from the wall clock, so runs land on the hour itself
    rather than an hour after whenever the container happened to start."""

    HOUR = 3600

    def test_on_the_hour_it_waits_a_full_hour(self):
        assert seconds_to_next_hour(10 * self.HOUR) == self.HOUR

    def test_one_minute_past_waits_fifty_nine(self):
        assert seconds_to_next_hour(10 * self.HOUR + 60) == self.HOUR - 60

    def test_the_delay_is_always_within_one_hour(self):
        for offset in range(0, self.HOUR, 137):
            assert 0 < seconds_to_next_hour(offset) <= self.HOUR

    def test_a_slow_run_does_not_push_the_next_one_later(self):
        """Two runs starting 40 minutes apart still land on the same boundary."""
        early = 10 * self.HOUR + 60
        late = 10 * self.HOUR + 2460  # 40 minutes later
        assert early + seconds_to_next_hour(early) == late + seconds_to_next_hour(late)
