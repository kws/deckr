"""Tests for the explicit core-vs-extension plugin protocol split."""

from deckr.plugin.messages import (
    CORE_COMMAND_MESSAGE_TYPES,
    DECKR_EXTENSION_COMMAND_MESSAGE_TYPES,
    DynamicPageDescriptor,
    SET_IMAGE,
    SET_PAGE,
    SlotBinding,
    TitleOptions,
)
from deckr.plugin.metadata import build_action_metadata


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
    wire = title_options.to_dict()
    assert wire == {
        "fontFamily": "Audiowide",
        "fontSize": "85vw",
        "fontStyle": "Bold",
        "titleColor": "#FFFFFF",
        "titleAlignment": "middle",
    }
    assert TitleOptions.model_validate(wire) == title_options


def test_title_options_omits_unset_fields_on_wire():
    assert TitleOptions(font_family="Inter").to_dict() == {"fontFamily": "Inter"}


def test_dynamic_page_descriptor_round_trip_on_wire():
    descriptor = DynamicPageDescriptor(
        page_id="page-1",
        slots=[
            SlotBinding(
                slot_id="0,0",
                action_uuid="com.example.action",
                settings={"album": "Kind of Blue"},
                title_options=TitleOptions(font_family="Inter"),
            )
        ],
    )

    wire = descriptor.to_dict()

    assert wire == {
        "pageId": "page-1",
        "slots": [
            {
                "slotId": "0,0",
                "actionUuid": "com.example.action",
                "settings": {"album": "Kind of Blue"},
                "titleOptions": {"fontFamily": "Inter"},
            }
        ],
    }
    assert DynamicPageDescriptor.model_validate(wire) == descriptor


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
