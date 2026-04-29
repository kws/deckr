from __future__ import annotations

import json
from pathlib import Path

from deckr.hardware.descriptors import (
    CAPABILITY_DESCRIPTOR_SCHEMA_ID,
    CONTROL_DESCRIPTOR_SCHEMA_ID,
    DEVICE_DESCRIPTOR_SCHEMA_ID,
    descriptor_schema_artifacts,
)

SCHEMA_FILENAMES = {
    DEVICE_DESCRIPTOR_SCHEMA_ID: "device-descriptor.v1.schema.json",
    CONTROL_DESCRIPTOR_SCHEMA_ID: "control-descriptor.v1.schema.json",
    CAPABILITY_DESCRIPTOR_SCHEMA_ID: "capability-descriptor.v1.schema.json",
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_dir = root / "schemas" / "hardware"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for schema_id, schema in descriptor_schema_artifacts().items():
        output = schema_dir / SCHEMA_FILENAMES[schema_id]
        output.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
