from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deckr.hardware.capabilities import (
    button_activation_value_schema,
    button_momentary_value_schema,
    encoder_relative_value_schema,
    raster_bitmap_command_schema,
    touch_gesture_value_schema,
)

JsonMap = Mapping[str, Any]


def _activation_schema() -> dict[str, Any]:
    return button_activation_value_schema().model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def _momentary_schema() -> dict[str, Any]:
    return button_momentary_value_schema().model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def _encoder_schema() -> dict[str, Any]:
    return encoder_relative_value_schema().model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def _touch_schema() -> dict[str, Any]:
    return touch_gesture_value_schema().model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def _raster_schema(width: int, height: int) -> dict[str, Any]:
    return raster_bitmap_command_schema(width=width, height=height).model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def _activation_capability(
    capability_id: str = "button.press",
    *,
    projection: JsonMap | None = None,
) -> dict[str, Any]:
    capability = {
        "capabilityId": capability_id,
        "family": "dev.deckr.input.button",
        "type": "activation",
        "direction": "input",
        "access": ["emits"],
        "valueSchema": _activation_schema(),
        "eventTypes": ["press"],
    }
    if projection is not None:
        capability["projection"] = dict(projection)
    return capability


def _momentary_capability(
    capability_id: str = "button.momentary",
) -> dict[str, Any]:
    return {
        "capabilityId": capability_id,
        "family": "dev.deckr.input.button",
        "type": "momentary",
        "direction": "input",
        "access": ["emits"],
        "valueSchema": _momentary_schema(),
        "eventTypes": ["down", "up"],
    }


def _encoder_capability(capability_id: str = "encoder.relative") -> dict[str, Any]:
    return {
        "capabilityId": capability_id,
        "family": "dev.deckr.input.encoder",
        "type": "relative",
        "direction": "input",
        "access": ["emits"],
        "valueSchema": _encoder_schema(),
        "eventTypes": ["rotate"],
        "constraints": [
            {
                "type": "range",
                "subject": "delta",
                "minimum": -24,
                "maximum": 24,
                "step": 1,
                "unit": "detent",
            }
        ],
        "units": [{"subject": "delta", "unit": "detent"}],
    }


def _touch_capability(capability_id: str = "touch.gesture") -> dict[str, Any]:
    return {
        "capabilityId": capability_id,
        "family": "dev.deckr.input.touch",
        "type": "gesture",
        "direction": "input",
        "access": ["emits"],
        "valueSchema": _touch_schema(),
        "eventTypes": ["tap", "swipe"],
    }


def _raster_capability(
    width: int,
    height: int,
    *,
    capability_id: str = "raster.bitmap",
    rotation: int = 0,
) -> dict[str, Any]:
    return {
        "capabilityId": capability_id,
        "family": "dev.deckr.output.raster",
        "type": "bitmap",
        "direction": "output",
        "access": ["settable"],
        "commandSchema": _raster_schema(width, height),
        "commandTypes": ["set_frame", "clear"],
        "constraints": [
            {"type": "fixed", "subject": "width", "value": width, "unit": "pixel"},
            {"type": "fixed", "subject": "height", "value": height, "unit": "pixel"},
            {
                "type": "enum",
                "subject": "encoding",
                "values": ["jpeg", "png"],
            },
            {
                "type": "fixed",
                "subject": "rotation",
                "value": rotation,
                "unit": "degree",
            },
        ],
        "units": [
            {"subject": "width", "unit": "pixel"},
            {"subject": "height", "unit": "pixel"},
            {"subject": "rotation", "unit": "degree"},
        ],
    }


def stream_deck_bitmap_grid() -> dict[str, Any]:
    controls = []
    for row in range(2):
        for column in range(3):
            control_id = f"key.{column}.{row}"
            controls.append(
                {
                    "controlId": control_id,
                    "kind": "bitmap_key",
                    "label": f"Key {column},{row}",
                    "geometry": {
                        "x": column,
                        "y": row,
                        "width": 1,
                        "height": 1,
                        "unit": "grid",
                    },
                    "inputCapabilities": [
                        _momentary_capability(),
                        _activation_capability(
                            projection={
                                "owner": "hardware_manager",
                                "source": {
                                    "controlId": control_id,
                                    "capabilityId": "button.momentary",
                                },
                            }
                        ),
                    ],
                    "outputCapabilities": [_raster_capability(72, 72, rotation=180)],
                    "sources": [
                        {
                            "sourceId": f"key-report-{column}-{row}",
                            "type": "hid",
                            "connectionId": "usb-hid-0",
                            "facts": {"keyIndex": row * 3 + column},
                        }
                    ],
                }
            )
    return {
        "deviceId": "stream-deck-mini",
        "fingerprint": "usb:0fd9:0063:serial-abc",
        "displayName": "Stream Deck Mini",
        "manufacturer": "Elgato",
        "model": "Stream Deck Mini",
        "modelId": "20GAA9901",
        "serialNumber": "serial-abc",
        "identifiers": [
            {
                "type": "usb.vendor_product",
                "namespace": "usb",
                "value": "0fd9:0063",
            }
        ],
        "connections": [
            {
                "connectionId": "usb-hid-0",
                "type": "hid",
                "status": "connected",
                "transport": "usb",
                "facts": {
                    "vendorId": 4057,
                    "productId": 99,
                    "serialNumber": "serial-abc",
                    "usagePage": 65280,
                    "usage": 1,
                    "reportDescriptorHash": "sha256:stream-deck-mini",
                },
            }
        ],
        "defaultStatusIndicator": {
            "controlId": "key.0.0",
            "capabilityId": "raster.bitmap",
        },
        "controls": controls,
    }


def mirabox_compound_dial_touch_surface() -> dict[str, Any]:
    return {
        "deviceId": "mars-gaming-msd-two",
        "fingerprint": "usb:0b00:1001:msd-two-001",
        "displayName": "Mars Gaming MSD-TWO",
        "manufacturer": "Mars Gaming",
        "model": "MSD-TWO",
        "firmwareVersion": "V25.MSD_TWO.01.005",
        "connections": [
            {
                "connectionId": "usb-hid-0",
                "type": "hid",
                "status": "connected",
                "transport": "usb",
                "facts": {
                    "vendorId": 2816,
                    "productId": 4097,
                    "interfaceNumber": 0,
                    "usagePage": 65440,
                },
            }
        ],
        "defaultStatusIndicator": {
            "controlId": "key.0.0",
            "capabilityId": "raster.bitmap",
        },
        "controls": [
            {
                "controlId": "key.0.0",
                "kind": "bitmap_key",
                "groupId": "content-grid",
                "surfaceId": "main-display",
                "geometry": {"x": 0, "y": 0, "width": 1, "height": 1, "unit": "grid"},
                "inputCapabilities": [_momentary_capability(), _activation_capability()],
                "outputCapabilities": [_raster_capability(96, 96)],
            },
            {
                "controlId": "dial.0",
                "kind": "rotary_encoder",
                "groupId": "dial-strip",
                "surfaceId": "dial-strip",
                "geometry": {"x": 0, "y": 2, "width": 1, "height": 1, "unit": "grid"},
                "inputCapabilities": [
                    _encoder_capability(),
                    _momentary_capability("encoder.button"),
                    _activation_capability(
                        "encoder.press",
                        projection={
                            "owner": "hardware_manager",
                            "source": {
                                "controlId": "dial.0",
                                "capabilityId": "encoder.button",
                            },
                        },
                    ),
                ],
                "outputCapabilities": [_raster_capability(240, 240, capability_id="dial.raster")],
            },
            {
                "controlId": "touch.0",
                "kind": "touch_surface",
                "parentControlId": "dial.0",
                "relatedControlIds": ["dial.0"],
                "groupId": "dial-strip",
                "surfaceId": "dial-strip",
                "geometry": {"x": 0, "y": 2, "width": 1, "height": 0.25, "unit": "grid"},
                "inputCapabilities": [_touch_capability()],
            },
            {
                "controlId": "button.home",
                "kind": "button",
                "groupId": "hardware-buttons",
                "geometry": {"x": 0, "y": 3, "width": 1, "height": 1, "unit": "grid"},
                "inputCapabilities": [_activation_capability()],
            },
        ],
    }


def plain_button_device() -> dict[str, Any]:
    return {
        "deviceId": "remote-button-pad",
        "fingerprint": "virtual:remote-button-pad",
        "displayName": "Remote Button Pad",
        "connections": [{"connectionId": "virtual-0", "type": "virtual"}],
        "controls": [
            {
                "controlId": "button.1",
                "kind": "button",
                "inputCapabilities": [_activation_capability()],
            }
        ],
    }


def press_only_button_control() -> dict[str, Any]:
    payload = plain_button_device()
    payload["deviceId"] = "press-only-button"
    payload["fingerprint"] = "virtual:press-only-button"
    payload["displayName"] = "Press-Only Button"
    return payload


def momentary_button_with_press_projection() -> dict[str, Any]:
    return {
        "deviceId": "momentary-button",
        "fingerprint": "virtual:momentary-button",
        "displayName": "Momentary Button",
        "connections": [{"connectionId": "virtual-0", "type": "virtual"}],
        "controls": [
            {
                "controlId": "button.1",
                "kind": "button",
                "inputCapabilities": [
                    _momentary_capability(),
                    _activation_capability(
                        projection={
                            "owner": "hardware_manager",
                            "source": {
                                "controlId": "button.1",
                                "capabilityId": "button.momentary",
                            },
                        },
                    ),
                ],
            }
        ],
    }


def touch_surface_with_tap_and_swipe() -> dict[str, Any]:
    return {
        "deviceId": "touch-surface",
        "fingerprint": "virtual:touch-surface",
        "displayName": "Touch Surface",
        "connections": [{"connectionId": "virtual-0", "type": "virtual"}],
        "controls": [
            {
                "controlId": "touch.main",
                "kind": "touch_surface",
                "geometry": {"x": 0, "y": 0, "width": 1, "height": 1, "unit": "normalized"},
                "inputCapabilities": [_touch_capability()],
            }
        ],
    }


def extension_capability_with_standard_projection() -> dict[str, Any]:
    native_capability = {
        "capabilityId": "smart.button",
        "family": "com.example.input.smart_button",
        "type": "action",
        "direction": "input",
        "access": ["emits"],
        "valueSchema": {
            "schemaId": "com.example.input.smart_button.value.v1",
            "schema": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"enum": ["single", "double", "hold", "release"]}
                },
                "additionalProperties": False,
            },
        },
        "eventTypes": ["single", "double", "hold", "release"],
    }
    return {
        "deviceId": "smart-button",
        "fingerprint": "com.example:smart-button:001",
        "displayName": "Smart Button",
        "connections": [{"connectionId": "vendor-radio-0", "type": "radio"}],
        "controls": [
            {
                "controlId": "button.smart",
                "kind": "button",
                "inputCapabilities": [
                    native_capability,
                    _activation_capability(
                        projection={
                            "owner": "hardware_manager",
                            "source": {
                                "controlId": "button.smart",
                                "capabilityId": "smart.button",
                            },
                        }
                    ),
                ],
            }
        ],
    }


def mqtt_zigbee_button() -> dict[str, Any]:
    return {
        "deviceId": "zigbee-remote-0x0330",
        "fingerprint": "zigbee2mqtt:remote:0x0330",
        "displayName": "Zigbee2MQTT Remote Button",
        "connections": [
            {
                "connectionId": "mqtt-action-topic",
                "type": "mqtt",
                "status": "connected",
                "transport": "mqtt",
                "facts": {
                    "topic": "zigbee2mqtt/remote/0x0330/action",
                    "payloadShape": "zigbee2mqtt-action",
                },
            }
        ],
        "sources": [
            {
                "sourceId": "zigbee2mqtt-action-payload",
                "type": "mqtt",
                "connectionId": "mqtt-action-topic",
                "facts": {
                    "actionField": "action",
                    "batteryFieldObserved": True,
                },
            }
        ],
        "controls": [
            {
                "controlId": "button.brightness_up",
                "kind": "button",
                "inputCapabilities": [_activation_capability()],
            }
        ],
    }


def descriptor_payloads() -> dict[str, dict[str, Any]]:
    return {
        "stream_deck_bitmap_grid": stream_deck_bitmap_grid(),
        "mirabox_compound_dial_touch_surface": mirabox_compound_dial_touch_surface(),
        "plain_button_device": plain_button_device(),
        "press_only_button_control": press_only_button_control(),
        "momentary_button_with_press_projection": momentary_button_with_press_projection(),
        "touch_surface_with_tap_and_swipe": touch_surface_with_tap_and_swipe(),
        "extension_capability_with_standard_projection": (
            extension_capability_with_standard_projection()
        ),
        "mqtt_zigbee_button": mqtt_zigbee_button(),
    }
