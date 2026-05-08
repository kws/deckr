from __future__ import annotations

import logging

_DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DEFAULT_DATE_FORMAT = "%H:%M:%S"
_QUIET_DEFAULT_LOGGER_LEVELS = {
    "websockets.server": logging.WARNING,
}


def _configure_quiet_default_loggers(level: int) -> None:
    if level <= logging.DEBUG:
        return
    for logger_name, quiet_level in _QUIET_DEFAULT_LOGGER_LEVELS.items():
        logger = logging.getLogger(logger_name)
        if logger.level == logging.NOTSET:
            logger.setLevel(quiet_level)


def configure_process_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    root = logging.getLogger()
    if root.handlers:
        return
    _configure_quiet_default_loggers(level)
    logging.basicConfig(
        level=level,
        format=_DEFAULT_LOG_FORMAT,
        datefmt=_DEFAULT_DATE_FORMAT,
    )
