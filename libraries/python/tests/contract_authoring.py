from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from repo_paths import deckr_repo_root


def schema_with_authoring_metadata(
    schema_path: str,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    metadata_path = (
        deckr_repo_root()
        / "contract"
        / "authoring"
        / "v1"
        / "schema-metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))["schemas"]
    enriched = copy.deepcopy(dict(schema))
    for pointer, overlay in metadata.get(schema_path, {}).items():
        target = _resolve_json_pointer(enriched, pointer)
        if not isinstance(target, dict):
            raise TypeError(f"{schema_path} {pointer}: target is not an object")
        target.update(copy.deepcopy(overlay))
    return enriched


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    current = document
    for raw_token in pointer.removeprefix("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        current = current[token]
    return current
