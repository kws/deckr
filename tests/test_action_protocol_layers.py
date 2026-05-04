"""Tests for the explicit core-vs-extension action provider protocol split."""

from deckr.actions import messages as actions
from deckr.actions.messages import (
    ACTION_PROVIDER_COMMAND_MESSAGE_TYPES,
    BINDING_ATTACHED,
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES,
    SETTINGS_PATCH,
    SETTINGS_REQUEST,
    SETTINGS_SNAPSHOT,
    ActionDescriptor,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
    TitleOptions,
)


def test_core_and_extension_command_sets_are_explicit():
    assert BINDING_OUTPUT in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert SETTINGS_REQUEST in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert SETTINGS_PATCH in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert "setPage" not in CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES
    assert "setPage" not in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert BINDING_ATTACHED not in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES


def test_v1_message_types_are_registered_with_lane_contract():
    from deckr.contracts.lanes import ACTION_MESSAGE_TYPES

    assert BINDING_ATTACHED in ACTION_MESSAGE_TYPES
    assert CAPABILITY_INPUT in ACTION_MESSAGE_TYPES
    assert BINDING_OUTPUT in ACTION_MESSAGE_TYPES
    assert SETTINGS_SNAPSHOT in ACTION_MESSAGE_TYPES


def test_removed_power_commands_are_not_action_lane_contracts():
    from deckr.contracts.lanes import ACTION_MESSAGE_TYPES

    assert "sleepScreen" not in ACTION_MESSAGE_TYPES
    assert "wakeScreen" not in ACTION_MESSAGE_TYPES
    assert "requestSettings" not in ACTION_MESSAGE_TYPES
    assert "setSettings" not in ACTION_MESSAGE_TYPES
    assert "hereAreSettings" not in ACTION_MESSAGE_TYPES
    assert "setPage" not in ACTION_MESSAGE_TYPES
    assert not hasattr(actions, "SLEEP_SCREEN")
    assert not hasattr(actions, "WAKE_SCREEN")
    assert not hasattr(actions, "REQUEST_SETTINGS")
    assert not hasattr(actions, "SET_SETTINGS")
    assert not hasattr(actions, "HERE_ARE_SETTINGS")
    assert not hasattr(actions, "SET_PAGE")


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
                target=PageChildBindingTarget(kind="self"),
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
                "target": {"kind": "self"},
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
        actionId="com.example.provider.action",
        name="Example Action",
        providerId="com.example.provider",
        settingsSchema={"type": "object"},
    )
    wire = descriptor.to_dict()
    assert wire == {
        "actionId": "com.example.provider.action",
        "name": "Example Action",
        "providerId": "com.example.provider",
        "settingsSchema": {"type": "object"},
    }
    assert ActionDescriptor.model_validate(wire) == descriptor
