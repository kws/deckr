"""Tests for the explicit core-vs-extension plugin protocol split."""

from deckr.plugin.messages import (
    CORE_COMMAND_MESSAGE_TYPES,
    DECKR_EXTENSION_COMMAND_MESSAGE_TYPES,
    SET_IMAGE,
    SET_PAGE,
)
from deckr.plugin.metadata import build_action_metadata
from deckr.plugin.rendering import (
    TitleOptions,
    title_options_from_wire,
    title_options_to_wire,
)


def test_core_and_extension_command_sets_are_explicit():
    assert SET_IMAGE in CORE_COMMAND_MESSAGE_TYPES
    assert SET_PAGE in DECKR_EXTENSION_COMMAND_MESSAGE_TYPES
    assert SET_PAGE not in CORE_COMMAND_MESSAGE_TYPES


def test_title_options_round_trip_on_wire():
    title_options = TitleOptions(
        font_family="Audiowide",
        font_size="85vw",
        font_style="Bold",
        title_color="#FFFFFF",
        title_alignment="middle",
    )
    wire = title_options_to_wire(title_options)
    assert wire == {
        "fontFamily": "Audiowide",
        "fontSize": "85vw",
        "fontStyle": "Bold",
        "titleColor": "#FFFFFF",
        "titleAlignment": "middle",
    }
    assert title_options_from_wire(wire) == title_options


def test_title_options_from_wire_ignores_empty_payload():
    assert title_options_from_wire({}) is None


def test_build_action_metadata_uses_explicit_action_fields():
    action = type(
        "Action",
        (),
        {
            "uuid": "com.example.plugin.action",
            "name": "Example Action",
            "plugin_uuid": "com.example.plugin",
        },
    )()
    assert build_action_metadata(action) == {
        "uuid": "com.example.plugin.action",
        "name": "Example Action",
        "pluginUuid": "com.example.plugin",
    }
