from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from deckr.contracts.lanes import DEFAULT_MESSAGE_CONTRACT_REGISTRY
from deckr.contracts.messages import service_address
from deckr.core.config import ConfigDocument
from deckr.launcher import build_runtime_substrate
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.nats_kv import KvBucketPolicy
from deckr.substrates.supervised_nats import (
    NatsServerBinaryResolutionError,
    NatsServerBinaryResolver,
    NatsServerHandle,
    NatsServerProcessExited,
    NatsServerStartupError,
    NatsServerSupervisor,
    NatsServerVersionError,
    ResolvedNatsServerBinary,
    SupervisedNatsSubstrate,
    _config_text,
    _nats_url_from_ports_file,
    _read_nats_server_version,
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


def test_nats_config_omits_authorization_when_auth_disabled() -> None:
    text = _config_text(
        server_name="deckr-test",
        host="127.0.0.1",
        port=4222,
        ports_dir=Path("/tmp/deckr/ports"),
        store_dir=Path("/tmp/deckr/jetstream"),
        auth_token=None,
    )

    assert 'host: "127.0.0.1"' in text
    assert "port: 4222" in text
    assert "authorization {" not in text
    assert "token:" not in text


def test_configured_nats_server_path_must_be_absolute() -> None:
    resolver = NatsServerBinaryResolver(server_path="bin/nats-server")

    with pytest.raises(NatsServerBinaryResolutionError, match="absolute"):
        resolver.resolve()


def test_nats_binary_resolver_reports_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deckr.substrates.supervised_nats._bundled_nats_server_path",
        lambda: None,
    )
    monkeypatch.setattr("deckr.substrates.supervised_nats.shutil.which", lambda _: None)

    with pytest.raises(NatsServerBinaryResolutionError, match="No configured"):
        NatsServerBinaryResolver().resolve()


def test_nats_binary_resolver_rejects_missing_and_non_executable_paths(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-nats-server"
    with pytest.raises(NatsServerBinaryResolutionError, match="does not exist"):
        NatsServerBinaryResolver(server_path=missing).resolve()

    not_executable = tmp_path / "nats-server"
    not_executable.write_text("#!/bin/sh\n")
    not_executable.chmod(0o600)

    with pytest.raises(NatsServerBinaryResolutionError, match="not executable"):
        NatsServerBinaryResolver(server_path=not_executable).resolve()


def test_nats_binary_resolver_rejects_bad_or_old_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "nats-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)

    monkeypatch.setattr(
        "deckr.substrates.supervised_nats.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "nats-server development build", "stderr": ""},
        )(),
    )
    with pytest.raises(NatsServerVersionError, match="Could not determine"):
        _read_nats_server_version(binary)

    monkeypatch.setattr(
        "deckr.substrates.supervised_nats.subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "nats-server: v2.13.9", "stderr": ""},
        )(),
    )
    with pytest.raises(NatsServerBinaryResolutionError, match="older than required"):
        NatsServerBinaryResolver(server_path=binary).resolve()


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"nats": []},
        {"nats": [123]},
        {"nats": ["http://127.0.0.1:4222"]},
    ),
)
def test_nats_ports_file_rejects_invalid_payloads(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    ports_file = tmp_path / "nats-server_123.ports"
    ports_file.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="nats URL|invalid nats URL"):
        _nats_url_from_ports_file(ports_file)


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
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    )

    assert isinstance(substrate, SupervisedNatsSubstrate)
    assert substrate.supervisor.runtime_dir == tmp_path / "run" / "nats"
    assert substrate.supervisor.store_dir == tmp_path / "state" / "nats"
    assert substrate.supervisor.auth_enabled is True
    assert substrate.supervisor.configured_auth_token is None
    assert substrate.supervisor.startup_timeout == 3.5
    assert substrate.supervisor.shutdown_timeout == 1.5


def test_runtime_substrate_config_builds_supervised_nats_without_auth(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "supervised": True,
                        "auth": False,
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    substrate = build_runtime_substrate(
        document,
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    )

    assert isinstance(substrate, SupervisedNatsSubstrate)
    assert substrate.supervisor.auth_enabled is False
    assert substrate.supervisor.configured_auth_token is None


def test_runtime_substrate_config_builds_supervised_nats_with_dev_token(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "supervised": True,
                        "auth": "token",
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    substrate = build_runtime_substrate(
        document,
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    )

    assert isinstance(substrate, SupervisedNatsSubstrate)
    assert substrate.supervisor.auth_enabled is True
    assert substrate.supervisor.configured_auth_token == "token"


def test_runtime_substrate_config_rejects_invalid_supervised_nats_auth(
    tmp_path: Path,
) -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "supervised": True,
                        "auth": True,
                    }
                }
            }
        },
        base_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="auth must be false"):
        build_runtime_substrate(
            document,
            lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        )


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
            lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
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
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
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
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
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
        "lane_contracts": DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        "buffer_size": 100,
    }


@pytest.mark.asyncio
async def test_supervisor_startup_timeout_includes_recent_logs(tmp_path: Path) -> None:
    supervisor = NatsServerSupervisor(
        startup_timeout=0.01,
        runtime_dir=tmp_path / "runtime",
    )
    supervisor._process = type("Process", (), {"returncode": None})()  # noqa: SLF001
    supervisor._ports_dir = tmp_path  # noqa: SLF001
    supervisor._logs.extend(("stdout: listening soon", "stderr: not ready"))  # noqa: SLF001

    with pytest.raises(NatsServerStartupError) as exc_info:
        await supervisor._wait_until_ready()  # noqa: SLF001

    assert "Recent nats-server output" in str(exc_info.value)
    assert "stdout: listening soon" in str(exc_info.value)


@pytest.mark.asyncio
async def test_supervisor_monitor_raises_on_unexpected_process_exit(
    tmp_path: Path,
) -> None:
    class Process:
        async def wait(self) -> int:
            return 7

    supervisor = NatsServerSupervisor(runtime_dir=tmp_path)
    supervisor._process = Process()  # noqa: SLF001
    supervisor._logs.append("stderr: fatal")  # noqa: SLF001

    with pytest.raises(NatsServerProcessExited) as exc_info:
        await supervisor._raise_on_unexpected_exit()  # noqa: SLF001

    assert "exited with code 7" in str(exc_info.value)
    assert "stderr: fatal" in str(exc_info.value)


@pytest.mark.asyncio
async def test_supervised_connect_failure_stops_supervisor_and_clears_nats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stopped: list[bool] = []

    class FakeSupervisor:
        async def start(self):
            return NatsServerHandle(
                url="nats://127.0.0.1:4222",
                auth_token=None,
                runtime_dir=tmp_path,
                config_path=tmp_path / "nats.conf",
                store_dir=tmp_path / "jetstream",
                binary=ResolvedNatsServerBinary(
                    path=tmp_path / "nats-server",
                    version="2.14.0",
                    source="test",
                ),
            )

        async def stop(self) -> None:
            stopped.append(True)

        @property
        def url(self):
            return None

    class FailingNatsSubstrate:
        def __init__(self, **_kwargs) -> None:
            return None

        async def connect(self) -> None:
            raise RuntimeError("connect failed")

    monkeypatch.setattr(
        "deckr.substrates.supervised_nats.NatsSubstrate",
        FailingNatsSubstrate,
    )
    substrate = SupervisedNatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        supervisor=FakeSupervisor(),
    )

    with pytest.raises(RuntimeError, match="connect failed"):
        await substrate.connect()

    assert stopped == [True]
    assert substrate._nats is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_supervised_delegated_operations_require_connection() -> None:
    substrate = SupervisedNatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        supervisor=object(),
    )

    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.publish(object())
    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.publish_reply(object(), request=object())
    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.request(object())
    with pytest.raises(RuntimeError, match="not connected"):
        substrate.subscribe(
            "actions",
            service_address("demo"),
            endpoint_session_id="session",
        )
    with pytest.raises(RuntimeError, match="not connected"):
        substrate.kv_bucket(KvBucketPolicy(bucket="views", ttl_seconds=None))
