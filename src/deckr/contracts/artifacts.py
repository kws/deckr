"""Helpers for reading generated Deckr contract artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

CONTRACT_ARTIFACT_VERSION = "v1"


def contract_bundle_root(version: str = CONTRACT_ARTIFACT_VERSION) -> Traversable:
    """Return the generated contract bundle root as a resource."""

    source_root = _source_checkout_bundle_root(version)
    if source_root.exists():
        return source_root
    return resources.files("deckr").joinpath("contract", version)


@contextmanager
def contract_bundle_path(
    version: str = CONTRACT_ARTIFACT_VERSION,
) -> Iterator[Path]:
    """Yield a filesystem path for the generated contract bundle."""

    root = contract_bundle_root(version)
    if isinstance(root, Path):
        yield root
        return
    with resources.as_file(root) as path:
        yield path


def contract_manifest(
    version: str = CONTRACT_ARTIFACT_VERSION,
) -> Mapping[str, Any]:
    """Read the generated contract bundle manifest."""

    root = contract_bundle_root(version)
    return json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))


def read_contract_artifact(
    relative_path: str,
    *,
    version: str = CONTRACT_ARTIFACT_VERSION,
) -> str:
    """Read a text artifact from the generated contract bundle."""

    root = contract_bundle_root(version)
    return root.joinpath(relative_path).read_text(encoding="utf-8")


def _source_checkout_bundle_root(version: str) -> Path:
    return Path(__file__).resolve().parents[3] / "contract" / version
