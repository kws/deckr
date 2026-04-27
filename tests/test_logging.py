from __future__ import annotations

import logging

from deckr.core.logging import configure_process_logging


def test_default_process_logging_quiets_websockets_server_info() -> None:
    root = logging.getLogger()
    original_root_level = root.level
    original_handlers = list(root.handlers)
    websockets_server = logging.getLogger("websockets.server")
    original_level = websockets_server.level
    root.handlers.clear()

    try:
        websockets_server.setLevel(logging.NOTSET)

        configure_process_logging("info")

        assert websockets_server.level == logging.WARNING
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(original_root_level)
        root.handlers[:] = original_handlers
        websockets_server.setLevel(original_level)


def test_debug_process_logging_keeps_websockets_server_verbose() -> None:
    root = logging.getLogger()
    original_root_level = root.level
    original_handlers = list(root.handlers)
    websockets_server = logging.getLogger("websockets.server")
    original_level = websockets_server.level
    root.handlers.clear()

    try:
        websockets_server.setLevel(logging.NOTSET)

        configure_process_logging("debug")

        assert websockets_server.level == logging.NOTSET
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(original_root_level)
        root.handlers[:] = original_handlers
        websockets_server.setLevel(original_level)
