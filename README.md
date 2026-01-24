# deckr

A Python library for controlling deck devices (Elgato Stream Deck, MiraBox etc)

## Overview

deckr is a Python library designed to provide a unified interface for controlling various dock-like devices such as the Elgato Stream Deck, MiraBox, and other similar hardware.

The library abstracts away the differences between various device manufacturers and provides a consistent API for button management, display control, and event handling across supported devices.

## Installation

### Using Poetry

This project uses [Poetry](https://python-poetry.org/) for dependency management and packaging.

**Install dependencies:**
```bash
poetry install
```

**Build the package:**
```bash
poetry build
```

This will create both a source distribution (`.tar.gz`) and a wheel (`.whl`) in the `dist/` directory.

**Install the package in development mode:**
```bash
poetry install
```

**Run tests:**
```bash
poetry run pytest
```

**Run linting:**
```bash
poetry run ruff check .
```

## Requirements

- Python >= 3.11
- Poetry >= 2.0.0

