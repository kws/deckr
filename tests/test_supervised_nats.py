from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from deckr.contracts.lanes import DEFAULT_LANE_CONTRACT_REGISTRY
from deckr.core.config import ConfigDocument
from deckr.launcher import build_runtime_substrate
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.supervised_nats import (
    NatsServerBinaryResolutionError,
    NatsServerBinaryResolver,
    NatsServerHandle,
    ResolvedNatsServerBinary,
    SupervisedNatsSubstrate,
    _config_text,
    _nats_url_from_ports_file,
)


def _document(raw: dict, *, base_dir: Path) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=base_dir)


def test_nats_ports_file_reports_selected_client_url(tmp_path: Path) -> None:
    ports_file = tmp_path / "nats-server_123.ports"
    ports_file.write_text(json.dumps({"nats": ["nats://127.0.0.1:52843"]}))

    assert _nats_url_from_ports_file(ports_file) == "nats://127.0.0.1:52843"


def test_nats_config_keeps_auth_token_in_private_config() -> None:
    text = _config_text(
        server_name="deckr-test",
        host="127.0.0.1",
        port=-1,
        ports_dir=Path("/tmp/deckr/ports"),
        store_dir=Path("/tmp/deckr/jetstream"),
        auth_token="secret-token",
    )

    assert 'host: "127.0.0.1"' in text
    assert "port: -1" in text
    assert 'token: "secret-token"' in text
    assert "jetstream {" in text
    assert "ports_file_dir" in text


def test_configured_nats_server_path_must_be_absolute() -> None:
    resolver = NatsServerBinaryResolver(server_path="bin/nats-server")

    with pytest.raises(NatsServerBinaryResolutionError, match="absolute"):
        resolver.resolve()


def test_runtime_substrate_config_builds_supervised_nats(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "supervised": True,
                        "server_path": "/opt/nats/bin/nats-server",
                        "runtime_dir": "run/nats",
                        "store_dir": "state/nats",
                        "startup_timeout": 3.5,
                        "shutdown_timeout": 1.5,
                        "log_buffer_lines": 25,
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    substrate = build_runtime_substrate(
        document,
        lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY,
    )

    assert isinstance(substrate, SupervisedNatsSubstrate)
    assert substrate.supervisor.runtime_dir == tmp_path / "run" / "nats"
    assert substrate.supervisor.store_dir == tmp_path / "state" / "nats"
    assert substrate.supervisor.startup_timeout == 3.5
    assert substrate.supervisor.shutdown_timeout == 1.5


def test_runtime_substrate_config_rejects_url_for_supervised_nats(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "supervised": True,
                        "url": "nats://127.0.0.1:4222",
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="url is not used"):
        build_runtime_substrate(
            document,
            lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY,
        )


def test_runtime_substrate_config_builds_external_nats_with_auth_token(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "url": "nats://nats.example:4222",
                        "auth_token": "secret-token",
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    substrate = build_runtime_substrate(
        document,
        lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY,
    )

    assert isinstance(substrate, NatsSubstrate)
    assert substrate.url == "nats://nats.example:4222"
    assert substrate.auth_token == "secret-token"


@pytest.mark.asyncio
async def test_supervised_substrate_starts_supervisor_before_nats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeSupervisor:
        url = None

        async def start(self):
            self.url = "nats://127.0.0.1:4321"
            captured["started"] = True
            return NatsServerHandle(
                url=self.url,
                auth_token="secret-token",
                runtime_dir=tmp_path,
                config_path=tmp_path / "nats.conf",
                store_dir=tmp_path / "jetstream",
                binary=ResolvedNatsServerBinary(
                    path=tmp_path / "nats-server",
                    version="2.14.0",
                    source="test",
                ),
            )

        def start_monitor(self, tg) -> None:
            captured["task_group"] = tg

        async def stop(self) -> None:
            captured["stopped"] = True

    class FakeNatsSubstrate:
        def __init__(self, **kwargs):
            captured["nats_kwargs"] = kwargs

        async def connect(self) -> None:
            captured["nats_connected"] = True

        async def aclose(self) -> None:
            captured["nats_closed"] = True

    monkeypatch.setattr(
        "deckr.substrates.supervised_nats.NatsSubstrate",
        FakeNatsSubstrate,
    )
    substrate = SupervisedNatsSubstrate(
        lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY,
        supervisor=FakeSupervisor(),
    )

    await substrate.connect()
    async with anyio.create_task_group() as tg:
        substrate.start(tg)
        tg.cancel_scope.cancel()
    await substrate.aclose()

    assert captured["started"] is True
    assert captured["nats_connected"] is True
    assert captured["nats_closed"] is True
    assert captured["stopped"] is True
    assert captured["nats_kwargs"] == {
        "url": "nats://127.0.0.1:4321",
        "auth_token": "secret-token",
        "lane_contracts": DEFAULT_LANE_CONTRACT_REGISTRY,
        "buffer_size": 100,
    }
