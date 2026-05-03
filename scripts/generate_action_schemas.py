from __future__ import annotations

import json
from pathlib import Path

from deckr.actions.messages import action_message_schema


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_dir = root / "schemas" / "actions"
    schema_dir.mkdir(parents=True, exist_ok=True)
    output = schema_dir / "actions.v1.schema.json"
    output.write_text(
        json.dumps(action_message_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
