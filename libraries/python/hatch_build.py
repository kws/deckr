from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        contract_root = _contract_root(Path(self.root))
        build_data.setdefault("force_include", {})[str(contract_root)] = (
            "deckr/contract/v1"
        )


def _contract_root(project_root: Path) -> Path:
    candidates = (
        project_root / "contract" / "v1",
        project_root.parents[1] / "contract" / "v1",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Could not find Deckr contract/v1 bundle")
