"""Pytest configuration for deckr package tests."""

import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    """Use anyio as the async backend for all tests."""
    return "anyio"
