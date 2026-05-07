from __future__ import annotations

from pathlib import Path


def deckr_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (
            (parent / "contract" / "v1").is_dir()
            and (parent / "scripts" / "generate_contract_artifacts.py").is_file()
        ):
            return parent
    raise RuntimeError("Could not find Deckr repository root")


def contract_bundle_root() -> Path:
    return deckr_repo_root() / "contract" / "v1"
