"""Tests for the explicit core-vs-extension action provider protocol split."""

from deckr.actions import messages as actions
from deckr.actions.messages import (
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_REQUEST,
    ACTION_AVAILABILITY_SNAPSHOT,
    ACTION_INTEREST_UPDATE,
    ACTION_LIFECYCLE_REJECTED,
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
)


def test_core_and_extension_command_sets_are_explicit():
    assert BINDING_OUTPUT in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert SETTINGS_REQUEST in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert SETTINGS_PATCH in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert ACTION_LIFECYCLE_REJECTED in CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES
    assert "setPage" not in CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES
    assert "setPage" not in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES
    assert BINDING_ATTACHED not in ACTION_PROVIDER_COMMAND_MESSAGE_TYPES


def test_v1_message_types_are_registered_with_lane_contract():
    from deckr.contracts.lanes import ACTION_MESSAGE_TYPES

    assert BINDING_ATTACHED in ACTION_MESSAGE_TYPES
    assert CAPABILITY_INPUT in ACTION_MESSAGE_TYPES
    assert BINDING_OUTPUT in ACTION_MESSAGE_TYPES
    assert ACTION_LIFECYCLE_REJECTED in ACTION_MESSAGE_TYPES
    assert SETTINGS_SNAPSHOT in ACTION_MESSAGE_TYPES
    assert ACTION_AVAILABILITY_REQUEST in ACTION_MESSAGE_TYPES
    assert ACTION_AVAILABILITY_SNAPSHOT in ACTION_MESSAGE_TYPES
    assert ACTION_AVAILABILITY_CHANGED in ACTION_MESSAGE_TYPES
    assert ACTION_INTEREST_UPDATE in ACTION_MESSAGE_TYPES


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


def test_dynamic_page_command_round_trip_on_wire():
    descriptor = DynamicPageCommand(
        pageId="page-1",
        bindings=[
            PageChildBindingDescriptor(
                controlId="0,0",
                target=PageChildBindingTarget(kind="self"),
                itemKey="kind-of-blue",
                handler="album",
                settings={"album": "Kind of Blue"},
            )
        ],
    )

    wire = descriptor.to_dict()

    assert wire == {
        "pageId": "page-1",
        "bindings": [
            {
                "controlId": "0,0",
                "target": {"kind": "self"},
                "itemKey": "kind-of-blue",
                "handler": "album",
                "settings": {"album": "Kind of Blue"},
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
