"""Tests for the explicit core-vs-extension plugin protocol split."""

from deckr.plugin.extensions import CAPABILITY_IMAGE_GRAPH
from deckr.plugin.graph_image import (
    GRAPH_IMAGE_DATA_URI_PREFIX,
    wire_to_graph_image_data_uri,
)
from deckr.plugin.manifest import CoreManifest, Manifest
from deckr.plugin.messages import (
    CORE_COMMAND_MESSAGE_TYPES,
    DECKR_EXTENSION_COMMAND_MESSAGE_TYPES,
    HERE_ARE_GLOBAL_SETTINGS,
    REQUEST_GLOBAL_SETTINGS,
    SET_IMAGE,
    SET_GLOBAL_SETTINGS,
    SET_PAGE,
    SWITCH_TO_PROFILE,
)


def test_core_and_extension_command_sets_are_explicit():
    assert SET_IMAGE in CORE_COMMAND_MESSAGE_TYPES
    assert SWITCH_TO_PROFILE in CORE_COMMAND_MESSAGE_TYPES
    assert REQUEST_GLOBAL_SETTINGS in CORE_COMMAND_MESSAGE_TYPES
    assert SET_GLOBAL_SETTINGS in CORE_COMMAND_MESSAGE_TYPES
    assert SET_PAGE in DECKR_EXTENSION_COMMAND_MESSAGE_TYPES


def test_graph_image_extension_uses_set_image_data_uri():
    data_uri = wire_to_graph_image_data_uri({"graph": {"format": "invariant-graph"}})
    assert data_uri.startswith(GRAPH_IMAGE_DATA_URI_PREFIX)
    assert CAPABILITY_IMAGE_GRAPH == "deckr.image-graph"


def test_core_manifest_excludes_deckr_extensions():
    manifest = CoreManifest.model_validate({"Actions": []})
    assert manifest.actions == []
    assert not hasattr(manifest, "deckr_requires")


def test_manifest_keeps_deckr_extension_fields():
    manifest = Manifest.model_validate(
        {"Actions": [], "DeckrRequires": ["deckr.pages", "deckr.image-graph"]}
    )
    assert manifest.deckr_requires == ["deckr.pages", "deckr.image-graph"]


def test_manifest_round_trips_property_inspector_fields():
    manifest = Manifest.model_validate(
        {
            "Name": "Example Plugin",
            "UUID": "com.example.plugin",
            "Version": "1.2.3",
            "PropertyInspectorPath": "inspector/index.html",
            "Actions": [
                {
                    "Name": "Example Action",
                    "UUID": "com.example.plugin.action",
                    "PropertyInspectorPath": "inspector/action.html",
                    "Controllers": ["Keypad"],
                    "States": [{}],
                }
            ],
        }
    )

    assert manifest.name == "Example Plugin"
    assert manifest.uuid == "com.example.plugin"
    assert manifest.version == "1.2.3"
    assert manifest.property_inspector_path == "inspector/index.html"
    assert manifest.actions[0].property_inspector_path == "inspector/action.html"

    dumped = manifest.model_dump(by_alias=True)
    assert dumped["UUID"] == "com.example.plugin"
    assert dumped["PropertyInspectorPath"] == "inspector/index.html"
    assert (
        dumped["Actions"][0]["PropertyInspectorPath"]
        == "inspector/action.html"
    )


def test_global_settings_response_constant_is_stable():
    assert HERE_ARE_GLOBAL_SETTINGS == "hereAreGlobalSettings"
