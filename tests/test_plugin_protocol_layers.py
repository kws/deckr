"""Tests for the explicit core-vs-extension plugin protocol split."""

from deckr.pluginhost import messages as plugin_messages
from deckr.pluginhost.messages import (
    BINDING_ATTACHED,
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    CORE_COMMAND_MESSAGE_TYPES,
    DECKR_EXTENSION_COMMAND_MESSAGE_TYPES,
    SETTINGS_PATCH,
    SETTINGS_REQUEST,
    SETTINGS_SNAPSHOT,
    ActionDescriptor,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    TitleOptions,
)


def test_core_and_extension_command_sets_are_explicit():
    assert BINDING_OUTPUT in CORE_COMMAND_MESSAGE_TYPES
    assert SETTINGS_REQUEST in CORE_COMMAND_MESSAGE_TYPES
    assert SETTINGS_PATCH in CORE_COMMAND_MESSAGE_TYPES
    assert "setPage" not in DECKR_EXTENSION_COMMAND_MESSAGE_TYPES
    assert "setPage" not in CORE_COMMAND_MESSAGE_TYPES
    assert BINDING_ATTACHED not in CORE_COMMAND_MESSAGE_TYPES


def test_v1_message_types_are_registered_with_lane_contract():
    from deckr.contracts.lanes import PLUGIN_MESSAGE_TYPES

    assert BINDING_ATTACHED in PLUGIN_MESSAGE_TYPES
    assert CAPABILITY_INPUT in PLUGIN_MESSAGE_TYPES
    assert BINDING_OUTPUT in PLUGIN_MESSAGE_TYPES
    assert SETTINGS_SNAPSHOT in PLUGIN_MESSAGE_TYPES


def test_removed_power_commands_are_not_plugin_lane_contracts():
    from deckr.contracts.lanes import PLUGIN_MESSAGE_TYPES

    assert "sleepScreen" not in PLUGIN_MESSAGE_TYPES
    assert "wakeScreen" not in PLUGIN_MESSAGE_TYPES
    assert "requestSettings" not in PLUGIN_MESSAGE_TYPES
    assert "setSettings" not in PLUGIN_MESSAGE_TYPES
    assert "hereAreSettings" not in PLUGIN_MESSAGE_TYPES
    assert "setPage" not in PLUGIN_MESSAGE_TYPES
    assert not hasattr(plugin_messages, "SLEEP_SCREEN")
    assert not hasattr(plugin_messages, "WAKE_SCREEN")
    assert not hasattr(plugin_messages, "REQUEST_SETTINGS")
    assert not hasattr(plugin_messages, "SET_SETTINGS")
    assert not hasattr(plugin_messages, "HERE_ARE_SETTINGS")
    assert not hasattr(plugin_messages, "SET_PAGE")


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
        actionId="com.example.plugin.action",
        name="Example Action",
        pluginId="com.example.plugin",
        settingsSchema={"type": "object"},
    )
    wire = descriptor.to_dict()
    assert wire == {
        "actionId": "com.example.plugin.action",
        "name": "Example Action",
        "pluginId": "com.example.plugin",
        "settingsSchema": {"type": "object"},
    }
    assert ActionDescriptor.model_validate(wire) == descriptor
