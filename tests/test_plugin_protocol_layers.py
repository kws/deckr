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
    SET_IMAGE,
    SET_PAGE,
    SWITCH_TO_PROFILE,
)


def test_core_and_extension_command_sets_are_explicit():
    assert SET_IMAGE in CORE_COMMAND_MESSAGE_TYPES
    assert SWITCH_TO_PROFILE in CORE_COMMAND_MESSAGE_TYPES
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
