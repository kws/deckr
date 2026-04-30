"""Tests for the explicit core-vs-extension plugin protocol split."""

from deckr.pluginhost.messages import (
    BINDING_ATTACHED,
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    CORE_COMMAND_MESSAGE_TYPES,
    DECKR_EXTENSION_COMMAND_MESSAGE_TYPES,
    SET_PAGE,
    ActionDescriptor,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    TitleOptions,
)


def test_core_and_extension_command_sets_are_explicit():
    assert BINDING_OUTPUT in CORE_COMMAND_MESSAGE_TYPES
    assert SET_PAGE in DECKR_EXTENSION_COMMAND_MESSAGE_TYPES
    assert SET_PAGE not in CORE_COMMAND_MESSAGE_TYPES
    assert BINDING_ATTACHED not in CORE_COMMAND_MESSAGE_TYPES


def test_v1_message_types_are_registered_with_lane_contract():
    from deckr.contracts.lanes import PLUGIN_MESSAGE_TYPES

    assert BINDING_ATTACHED in PLUGIN_MESSAGE_TYPES
    assert CAPABILITY_INPUT in PLUGIN_MESSAGE_TYPES
    assert BINDING_OUTPUT in PLUGIN_MESSAGE_TYPES


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


def test_dynamic_page_command_round_trip_on_wire():
    descriptor = DynamicPageCommand(
        pageId="page-1",
        templateId="browser",
        bindings=[
            PageChildBindingDescriptor(
                controlId="0,0",
                roleId="album",
                itemKey="kind-of-blue",
                handler="album",
                settings={"album": "Kind of Blue"},
                title_options=TitleOptions(font_family="Inter"),
            )
        ],
    )

    wire = descriptor.to_dict()

    assert wire == {
        "pageId": "page-1",
        "templateId": "browser",
        "bindings": [
            {
                "controlId": "0,0",
                "roleId": "album",
                "itemKey": "kind-of-blue",
                "handler": "album",
                "settings": {"album": "Kind of Blue"},
                "titleOptions": {"fontFamily": "Inter"},
            }
        ],
    }
    assert DynamicPageCommand.model_validate(wire) == descriptor


def test_action_descriptor_round_trip_on_wire():
    descriptor = ActionDescriptor(
        uuid="com.example.plugin.action",
        name="Example Action",
        plugin_uuid="com.example.plugin",
    )
    wire = descriptor.to_dict()
    assert wire == {
        "uuid": "com.example.plugin.action",
        "name": "Example Action",
        "pluginUuid": "com.example.plugin",
    }
    assert ActionDescriptor.model_validate(wire) == descriptor
