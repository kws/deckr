"""Unit tests for plugin event models."""

from __future__ import annotations

from deckr.python_plugin.events import (
    DialRotate,
    KeyDown,
    PageAppear,
    SlotInfo,
    TouchSwipe,
    WillAppear,
    WillDisappear,
)


def test_slot_info_serializes_to_camel_case() -> None:
    slot = SlotInfo(
        slot_id="0,0",
        slot_type="key",
        coordinates={"column": 0, "row": 0},
        gestures=["key_down", "key_up"],
        image_format={"width": 72, "height": 72, "format": "JPEG", "rotation": 0},
    )

    data = slot.model_dump(by_alias=True, mode="json")

    assert data["slotId"] == "0,0"
    assert data["slotType"] == "key"
    assert data["gestures"] == ["key_down", "key_up"]
    assert data["coordinates"] == {"column": 0, "row": 0}
    assert data["imageFormat"] == {
        "width": 72,
        "height": 72,
        "format": "JPEG",
        "rotation": 0,
    }


def test_will_appear_uses_slot_metadata_instead_of_controller_payload() -> None:
    event = WillAppear(
        context="controller=a|config=b|slot=0%2C0",
        slot=SlotInfo(
            slot_id="0,0",
            slot_type="key",
            coordinates={"column": 0, "row": 0},
            gestures=["key_down", "key_up"],
        ),
    )

    dumped = event.model_dump(by_alias=True, mode="json")

    assert dumped == {
        "event": "willAppear",
        "context": "controller=a|config=b|slot=0%2C0",
        "slot": {
            "slotId": "0,0",
            "slotType": "key",
            "coordinates": {"column": 0, "row": 0},
            "gestures": ["key_down", "key_up"],
            "imageFormat": None,
        },
    }


def test_will_disappear_serializes_slot_id() -> None:
    event = WillDisappear(
        context="controller=a|config=b|slot=0%2C0",
        slot_id="0,0",
    )

    assert event.model_dump(by_alias=True) == {
        "event": "willDisappear",
        "context": "controller=a|config=b|slot=0%2C0",
        "slotId": "0,0",
    }


def test_interaction_events_use_slot_id_and_direction() -> None:
    key_event = KeyDown(context="ctx", slot_id="1,2")
    rotate_event = DialRotate(
        context="ctx",
        slot_id="dial-1",
        direction="counterclockwise",
    )
    swipe_event = TouchSwipe(
        context="ctx",
        slot_id="strip-1",
        direction="left",
    )

    assert key_event.model_dump(by_alias=True) == {
        "event": "keyDown",
        "context": "ctx",
        "slotId": "1,2",
    }
    assert rotate_event.model_dump(by_alias=True) == {
        "event": "dialRotate",
        "context": "ctx",
        "slotId": "dial-1",
        "direction": "counterclockwise",
    }
    assert swipe_event.model_dump(by_alias=True) == {
        "event": "touchSwipe",
        "context": "ctx",
        "slotId": "strip-1",
        "direction": "left",
    }


def test_page_appear_no_longer_embeds_action_or_settings() -> None:
    event = PageAppear(context="ctx", page_id="page-1", timeout_ms=2500)

    assert event.model_dump(by_alias=True) == {
        "event": "pageAppear",
        "context": "ctx",
        "pageId": "page-1",
        "timeoutMs": 2500,
    }
