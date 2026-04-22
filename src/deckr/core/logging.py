from __future__ import annotations

import logging

_DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DEFAULT_DATE_FORMAT = "%H:%M:%S"


def configure_process_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=level,
        format=_DEFAULT_LOG_FORMAT,
        datefmt=_DEFAULT_DATE_FORMAT,
    )
