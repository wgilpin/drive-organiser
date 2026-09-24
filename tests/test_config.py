"""Config parsing is pure and offline, so it is tested exhaustively.

Every failure mode here is one a person hits while editing config.toml by hand,
so each assertion also checks the message names the thing that is wrong.
"""

import pytest

from drive_organiser.config import ConfigError, parse_config

GOOD = """
[drive]
inbox_folder_id    = "inbox1"
unsorted_folder_id = "unsorted1"

[[destinations]]
id = "dest1"
name = "Academic"
description = "Papers and lecture notes."

[gemini]
min_confidence = 0.7

[limits]
max_files_per_run = 10
"""


def test_parses_a_good_config():
    cfg = parse_config(GOOD)
    assert cfg.inbox_folder_ids == ("inbox1",)
    assert cfg.unsorted_folder_id == "unsorted1"
    assert len(cfg.destinations) == 1
    assert cfg.destinations[0].name == "Academic"
    assert cfg.min_confidence == 0.7
    assert cfg.max_files_per_run == 10


def test_defaults_apply_when_sections_absent():
    cfg = parse_config(
        """
        [drive]
        inbox_folder_id = "i"
        unsorted_folder_id = "u"
        [[destinations]]
        id = "d"
        name = "N"
        description = "D"
        """
    )
    assert cfg.min_confidence == 0.6
    assert cfg.max_files_per_run == 50
    assert cfg.max_inline_bytes == 20_000_000
    assert cfg.email_on_empty is False
    assert cfg.gemini_model is None  # falls back to GEMINI_MODEL in .env


def test_folder_ids_maps_every_configured_folder():
    ids = parse_config(GOOD).folder_ids()
    assert ids == {"inbox1": "inbox", "unsorted1": "_Unsorted", "dest1": "Academic"}


def test_accepts_a_list_of_inboxes():
    cfg = parse_config(GOOD.replace('inbox_folder_id    = "inbox1"', 'inbox_folder_ids = ["inbox1", "inbox2"]'))
    assert cfg.inbox_folder_ids == ("inbox1", "inbox2")
    assert cfg.folder_ids()["inbox2"] == "inbox"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('inbox_folder_ids = []', "missing"),
        ('inbox_folder_ids = ["inbox1", ""]', "empty id"),
        ('inbox_folder_ids = ["inbox1", "inbox1"]', "more than once"),
        ('inbox_folder_ids = "inbox1"\ninbox_folder_id = "inbox1"', "not both"),
        ('inbox_folder_ids = ["unsorted1"]', "also _Unsorted"),
    ],
)
def test_rejects_a_bad_inbox_list(line, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(GOOD.replace('inbox_folder_id    = "inbox1"', line))


def test_rejects_a_second_inbox_listed_as_a_destination():
    text = GOOD.replace('inbox_folder_id    = "inbox1"', 'inbox_folder_ids = ["inbox1", "dest1"]')
    with pytest.raises(ConfigError, match="dest1 is also listed as a destination"):
        parse_config(text)


def test_rejects_invalid_toml():
    with pytest.raises(ConfigError, match="not valid TOML"):
        parse_config("[drive")


def test_rejects_missing_drive_section():
    with pytest.raises(ConfigError, match=r"\[drive\] section"):
        parse_config('[[destinations]]\nid="d"\nname="N"\ndescription="D"')


@pytest.mark.parametrize(
    ("key", "message"), [("inbox_folder_id", "inbox_folder_ids"), ("unsorted_folder_id", "unsorted_folder_id")]
)
def test_rejects_missing_drive_key(key, message):
    text = GOOD.replace(f'{key}    = ', f'{key}_x = ').replace(f'{key} = ', f'{key}_x = ')
    with pytest.raises(ConfigError, match=message):
        parse_config(text)


def test_rejects_config_with_no_destinations():
    with pytest.raises(ConfigError, match="no \\[\\[destinations\\]\\]"):
        parse_config('[drive]\ninbox_folder_id="i"\nunsorted_folder_id="u"')


@pytest.mark.parametrize("field", ["id", "name", "description"])
def test_rejects_destination_missing_a_field(field):
    text = GOOD.replace(f'{field} = ', f'{field}_x = ')
    with pytest.raises(ConfigError, match=field):
        parse_config(text)


def test_rejects_empty_description():
    """The generated template ships with description = "" — that must not pass."""
    with pytest.raises(ConfigError, match="description"):
        parse_config(GOOD.replace('description = "Papers and lecture notes."', 'description = ""'))


def test_rejects_duplicate_destination_ids():
    text = GOOD + '\n[[destinations]]\nid = "dest1"\nname = "Other"\ndescription = "d"\n'
    with pytest.raises(ConfigError, match="more than once"):
        parse_config(text)


def test_rejects_inbox_listed_as_a_destination():
    """Otherwise files could be 'filed' back into the inbox and loop forever."""
    text = GOOD + '\n[[destinations]]\nid = "inbox1"\nname = "Inbox"\ndescription = "d"\n'
    with pytest.raises(ConfigError, match="never leave"):
        parse_config(text)


def test_rejects_unsorted_listed_as_a_destination():
    text = GOOD + '\n[[destinations]]\nid = "unsorted1"\nname = "U"\ndescription = "d"\n'
    with pytest.raises(ConfigError, match="_Unsorted"):
        parse_config(text)


@pytest.mark.parametrize("bad", ["-0.1", "1.5"])
def test_rejects_out_of_range_confidence(bad):
    with pytest.raises(ConfigError, match="between 0 and 1"):
        parse_config(GOOD.replace("min_confidence = 0.7", f"min_confidence = {bad}"))
