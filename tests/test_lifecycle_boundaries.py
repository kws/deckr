from __future__ import annotations

import re
from pathlib import Path

_WORKSPACE_PROJECTS = (
    "deckr",
    "deckr-controller",
    "deckr-action-provider-runtime-python",
    "deckr-driver-elgato",
    "deckr-driver-mirabox",
    "deckr-driver-mqtt",
    "deckr-plugin-clock",
    "deckr-plugin-openhab",
    "deckr-plugin-sonos",
    "deckr-plugin-kaj",
)

_ALLOW_RAW_LIFECYCLE_STATE = {
    Path("deckr/src/deckr/beacon.py"),
    Path("deckr/src/deckr/concord.py"),
}

_FORBIDDEN_PATTERNS = (
    (
        re.compile(r"\.watch\(\s*['\"]advertisements\.by_feature\."),
        "direct Beacon authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*beacon_feature_prefix\("),
        "direct Beacon authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*concord_contracts_prefix\("),
        "direct Concord authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*['\"]contracts\."),
        "direct Concord authority state watch",
    ),
    (
        re.compile(r"\._(?:contract|token)_state\.watch\("),
        "direct Concord authority state watch",
    ),
)


