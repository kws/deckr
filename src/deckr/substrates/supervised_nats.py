from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections import deque
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from deckr.contracts.lanes import LaneContractRegistry
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.lanes import ReplyPredicate
from deckr.state import (
    DEFAULT_DISCOVERY_STATE_STORE_NAME,
    DEFAULT_LEASE_STATE_STORE_NAME,
    StateStore,
)
from deckr.substrates.nats import NatsSubstrate

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = -1
_DEFAULT_MINIMUM_VERSION = "2.14.0"
_VERSION_RE = re.compile(r"\bv(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")


class NatsServerSupervisorError(RuntimeError):
    """Base class for supervised nats-server failures."""


class NatsServerBinaryResolutionError(NatsServerSupervisorError):
    """Raised when no usable nats-server binary can be resolved."""


class NatsServerVersionError(NatsServerSupervisorError):
    """Raised when a resolved nats-server binary is too old or invalid."""


class NatsServerStartupError(NatsServerSupervisorError):
    """Raised when the supervised nats-server does not become ready."""


class NatsServerProcessExited(NatsServerSupervisorError):
    """Raised when nats-server exits while the supervised runtime is active."""


@dataclass(frozen=True, slots=True)
class ResolvedNatsServerBinary:
    path: Path
    version: str
    source: str


@dataclass(frozen=True, slots=True)
class NatsServerHandle:
    url: str
    auth_token: str
    runtime_dir: Path
    config_path: Path
    store_dir: Path
    binary: ResolvedNatsServerBinary


class NatsServerBinaryResolver:
    def __init__(
        self,
        *,
        server_path: Path | str | None = None,
        minimum_version: str = _DEFAULT_MINIMUM_VERSION,
    ) -> None:
        self.server_path = Path(server_path).expanduser() if server_path else None
        self.minimum_version = minimum_version

    def resolve(self) -> ResolvedNatsServerBinary:
        candidates = list(self._candidates())
        errors: list[str] = []
        for path, source in candidates:
            try:
                return self._resolve_candidate(path, source=source)
            except NatsServerSupervisorError as exc:
                errors.append(str(exc))
        if not errors:
            errors.append(
                "No configured, bundled, or system nats-server binary was found"
            )
        raise NatsServerBinaryResolutionError("; ".join(errors))

    def _candidates(self) -> Iterator[tuple[Path, str]]:
        if self.server_path is not None:
            if not self.server_path.is_absolute():
                raise NatsServerBinaryResolutionError(
                    "Configured nats-server path must be absolute"
                )
            yield self.server_path, "configured"
            return

        bundled = _bundled_nats_server_path()
        if bundled is not None:
            yield bundled, "bundled"

        system = shutil.which("nats-server")
        if system:
            yield Path(system), "path"

    def _resolve_candidate(
        self,
        path: Path,
        *,
        source: str,
    ) -> ResolvedNatsServerBinary:
        if not path.exists():
            raise NatsServerBinaryResolutionError(
                f"nats-server binary does not exist: {path}"
            )
        if not os.access(path, os.X_OK):
            raise NatsServerBinaryResolutionError(
                f"nats-server binary is not executable: {path}"
            )
        version = _read_nats_server_version(path)
        _assert_minimum_version(version, self.minimum_version, path=path)
        return ResolvedNatsServerBinary(path=path, version=version, source=source)


class NatsServerSupervisor:
    def __init__(
        self,
        *,
        server_path: Path | str | None = None,
        minimum_version: str = _DEFAULT_MINIMUM_VERSION,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        runtime_dir: Path | str | None = None,
        store_dir: Path | str | None = None,
        server_name: str = "deckr-local-nats",
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        log_buffer_lines: int = 200,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be greater than zero")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be greater than zero")
        if log_buffer_lines <= 0:
            raise ValueError("log_buffer_lines must be greater than zero")
        self._resolver = NatsServerBinaryResolver(
            server_path=server_path,
            minimum_version=minimum_version,
        )
        self.host = host
        self.port = int(port)
        self.runtime_dir = Path(runtime_dir).expanduser() if runtime_dir else None
        self.store_dir = Path(store_dir).expanduser() if store_dir else None
        self.server_name = server_name
        self.startup_timeout = float(startup_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self._logs: deque[str] = deque(maxlen=log_buffer_lines)
        self._temporary_dir: tempfile.TemporaryDirectory[str] | None = None
        self._process = None
        self._io_task_group_cm: AbstractAsyncContextManager[anyio.abc.TaskGroup] | None = (
            None
        )
        self._io_task_group: anyio.abc.TaskGroup | None = None
        self._handle: NatsServerHandle | None = None
        self._auth_token: str | None = None
        self._ports_dir: Path | None = None
        self._stopping = False

    @property
    def handle(self) -> NatsServerHandle | None:
        return self._handle

    @property
    def url(self) -> str | None:
        return self._handle.url if self._handle is not None else None

    @property
    def auth_token(self) -> str | None:
        return self._auth_token

    def recent_logs(self) -> tuple[str, ...]:
        return tuple(self._logs)

    async def start(self) -> NatsServerHandle:
        if self._process is not None:
            raise RuntimeError("nats-server supervisor is already running")

        binary = self._resolver.resolve()
        runtime_dir = self._prepare_runtime_dir()
        store_dir = self._prepare_store_dir(runtime_dir)
        ports_dir = runtime_dir / "ports"
        ports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _chmod_private_directory(ports_dir)
        _remove_stale_ports_files(ports_dir)

        token = secrets.token_urlsafe(32)
        config_path = runtime_dir / "nats-server.conf"
        _write_private_text(
            config_path,
            _config_text(
                server_name=self.server_name,
                host=self.host,
                port=self.port,
                ports_dir=ports_dir,
                store_dir=store_dir,
                auth_token=token,
            ),
        )

        self._auth_token = token
        self._ports_dir = ports_dir
        self._stopping = False
        try:
            self._process = await anyio.open_process(
                [str(binary.path), "-c", str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(runtime_dir),
                start_new_session=True,
            )
            await self._start_io_drain()
            url = await self._wait_until_ready()
        except BaseException:
            await self.stop()
            raise

        self._handle = NatsServerHandle(
            url=url,
            auth_token=token,
            runtime_dir=runtime_dir,
            config_path=config_path,
            store_dir=store_dir,
            binary=binary,
        )
        logger.info(
            "Started supervised nats-server %s from %s on %s",
            binary.version,
            binary.source,
            url,
        )
        return self._handle

    def start_monitor(self, tg: anyio.abc.TaskGroup) -> None:
        tg.start_soon(self._raise_on_unexpected_exit)

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None:
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(self.shutdown_timeout) as scope:
                    await process.wait()
                if scope.cancel_called and process.returncode is None:
                    process.kill()
                    await process.wait()
            with anyio.move_on_after(1.0, shield=True):
                await process.aclose()
        self._process = None

        if self._io_task_group is not None:
            self._io_task_group.cancel_scope.cancel()
        if self._io_task_group_cm is not None:
            await self._io_task_group_cm.__aexit__(None, None, None)
        self._io_task_group = None
        self._io_task_group_cm = None

        if self._ports_dir is not None:
            _remove_stale_ports_files(self._ports_dir)
        self._ports_dir = None
        self._handle = None
        self._auth_token = None

        if self._temporary_dir is not None:
            self._temporary_dir.cleanup()
            self._temporary_dir = None

    async def _start_io_drain(self) -> None:
        process = self._process
        if process is None:
            return
        self._io_task_group_cm = anyio.create_task_group()
        self._io_task_group = await self._io_task_group_cm.__aenter__()
        if process.stdout is not None:
            self._io_task_group.start_soon(self._drain_output, process.stdout, "stdout")
        if process.stderr is not None:
            self._io_task_group.start_soon(self._drain_output, process.stderr, "stderr")

    async def _drain_output(self, stream, source: str) -> None:
        pending = b""
        try:
            while True:
                chunk = await stream.receive()
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self._append_log(source, line)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream):
            pass
        finally:
            if pending:
                self._append_log(source, pending)

    def _append_log(self, source: str, line: bytes) -> None:
        rendered = line.decode("utf-8", errors="replace").rstrip("\r")
        if rendered:
            self._logs.append(f"{source}: {rendered}")

    async def _wait_until_ready(self) -> str:
        deadline = anyio.current_time() + self.startup_timeout
        last_error: BaseException | None = None
        while anyio.current_time() < deadline:
            process = self._process
            if process is None:
                raise NatsServerStartupError("nats-server process was not started")
            if process.returncode is not None:
                raise NatsServerStartupError(
                    _failure_message(
                        f"nats-server exited with code {process.returncode}",
                        logs=self.recent_logs(),
                    )
                )
            try:
                url = self._read_ports_file_url()
                if url is not None:
                    await _verify_jetstream(url, auth_token=self._auth_token)
                    return url
            except Exception as exc:
                last_error = exc
            await anyio.sleep(0.05)
        detail = "nats-server did not become ready"
        if last_error is not None:
            detail = f"{detail}: {last_error}"
        raise NatsServerStartupError(
            _failure_message(detail, logs=self.recent_logs())
        )

    def _read_ports_file_url(self) -> str | None:
        if self._ports_dir is None:
            return None
        for path in sorted(self._ports_dir.glob("*.ports")):
            try:
                return _nats_url_from_ports_file(path)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return None

    async def _raise_on_unexpected_exit(self) -> None:
        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._stopping:
            return
        raise NatsServerProcessExited(
            _failure_message(
                f"supervised nats-server exited with code {returncode}",
                logs=self.recent_logs(),
            )
        )

    def _prepare_runtime_dir(self) -> Path:
        if self.runtime_dir is not None:
            self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _chmod_private_directory(self.runtime_dir)
            return self.runtime_dir
        self._temporary_dir = tempfile.TemporaryDirectory(prefix="deckr-nats-")
        path = Path(self._temporary_dir.name)
        _chmod_private_directory(path)
        return path

    def _prepare_store_dir(self, runtime_dir: Path) -> Path:
        store_dir = self.store_dir or runtime_dir / "jetstream"
        store_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _chmod_private_directory(store_dir)
        return store_dir


class SupervisedNatsSubstrate:
    def __init__(
        self,
        *,
        lane_contracts: LaneContractRegistry,
        supervisor: NatsServerSupervisor | None = None,
        server_path: Path | str | None = None,
        minimum_server_version: str = _DEFAULT_MINIMUM_VERSION,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        runtime_dir: Path | str | None = None,
        store_dir: Path | str | None = None,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        log_buffer_lines: int = 200,
        buffer_size: int = 100,
        default_state_name: str = DEFAULT_LEASE_STATE_STORE_NAME,
        discovery_state_name: str = DEFAULT_DISCOVERY_STATE_STORE_NAME,
    ) -> None:
        self.default_state_name = default_state_name
        self.discovery_state_name = discovery_state_name
        self.supervisor = supervisor or NatsServerSupervisor(
            server_path=server_path,
            minimum_version=minimum_server_version,
            host=host,
            port=port,
            runtime_dir=runtime_dir,
            store_dir=store_dir,
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
            log_buffer_lines=log_buffer_lines,
        )
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._nats: NatsSubstrate | None = None

    @property
    def url(self) -> str | None:
        if self._nats is not None:
            return self._nats.url
        return self.supervisor.url

    async def connect(self) -> None:
        if self._nats is not None:
            raise RuntimeError("Supervised NATS substrate is already connected")
        handle = await self.supervisor.start()
        self._nats = NatsSubstrate(
            url=handle.url,
            auth_token=handle.auth_token,
            lane_contracts=self._lane_contracts,
            buffer_size=self._buffer_size,
            default_state_name=self.default_state_name,
            discovery_state_name=self.discovery_state_name,
        )
        try:
            await self._nats.connect()
        except BaseException:
            await self.supervisor.stop()
            self._nats = None
            raise

    def start(self, tg: anyio.abc.TaskGroup) -> None:
        self.supervisor.start_monitor(tg)

    async def aclose(self) -> None:
        try:
            if self._nats is not None:
                await self._nats.aclose()
        finally:
            self._nats = None
            await self.supervisor.stop()

    async def publish(self, message: DeckrMessage) -> None:
        await self._connected_nats().publish(message)

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None:
        await self._connected_nats().publish_reply(message, request=request)

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        return await self._connected_nats().request(
            message,
            timeout=timeout,
            accept=accept,
        )

    def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
        *,
        endpoint_session_id: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        return self._connected_nats().subscribe(
            lane,
            endpoint,
            endpoint_session_id=endpoint_session_id,
        )

    def state(self, name: str) -> StateStore:
        return self._connected_nats().state(name)

    def _connected_nats(self) -> NatsSubstrate:
        if self._nats is None:
            raise RuntimeError("Supervised NATS substrate is not connected")
        return self._nats


def _bundled_nats_server_path() -> Path | None:
    try:
        from deckr_nats_server_bin import (  # type: ignore[import-not-found]
            NatsServerBinaryNotFound,
            nats_server_path,
        )
    except ModuleNotFoundError:
        return None
    try:
        return nats_server_path()
    except NatsServerBinaryNotFound:
        return None


def _read_nats_server_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "-v"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NatsServerVersionError(
            f"Could not execute nats-server binary {path}: {exc}"
        ) from exc
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    match = _VERSION_RE.search(output)
    if result.returncode != 0 or match is None:
        raise NatsServerVersionError(
            f"Could not determine nats-server version from {path}"
        )
    return match.group("version")


def _assert_minimum_version(version: str, minimum: str, *, path: Path) -> None:
    if _version_key(version) < _version_key(minimum):
        raise NatsServerVersionError(
            f"nats-server {version} at {path} is older than required {minimum}"
        )


def _version_key(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch)


def _write_private_text(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _chmod_private_directory(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        logger.debug("Could not set private permissions on %s", path, exc_info=True)


def _remove_stale_ports_files(path: Path) -> None:
    for ports_file in path.glob("*.ports"):
        try:
            ports_file.unlink()
        except FileNotFoundError:
            pass


def _config_text(
    *,
    server_name: str,
    host: str,
    port: int,
    ports_dir: Path,
    store_dir: Path,
    auth_token: str,
) -> str:
    return "\n".join(
        (
            f"server_name: {json.dumps(server_name)}",
            f"host: {json.dumps(host)}",
            f"port: {port}",
            f"ports_file_dir: {json.dumps(str(ports_dir))}",
            "jetstream {",
            f"  store_dir: {json.dumps(str(store_dir))}",
            "}",
            "authorization {",
            f"  token: {json.dumps(auth_token)}",
            "}",
            "",
        )
    )


def _nats_url_from_ports_file(path: Path) -> str:
    payload = json.loads(path.read_text())
    urls = payload.get("nats")
    if not isinstance(urls, list) or not urls:
        raise ValueError(f"NATS ports file {path} does not contain a nats URL")
    url = urls[0]
    if not isinstance(url, str) or not url.startswith(("nats://", "tls://")):
        raise ValueError(f"NATS ports file {path} contains an invalid nats URL")
    return url


async def _verify_jetstream(url: str, *, auth_token: str | None) -> None:
    try:
        import nats
    except ModuleNotFoundError as exc:
        raise RuntimeError("Supervised NATS requires deckr[nats].") from exc

    connect_options: dict[str, Any] = {
        "allow_reconnect": False,
        "connect_timeout": 1,
        "max_reconnect_attempts": 0,
    }
    if auth_token is not None:
        connect_options["token"] = auth_token
    nc = await nats.connect(url, **connect_options)
    try:
        await nc.jetstream().account_info()
    finally:
        await nc.close()


def _failure_message(message: str, *, logs: tuple[str, ...]) -> str:
    if not logs:
        return message
    recent = "\n".join(f"  {line}" for line in logs[-20:])
    return f"{message}\nRecent nats-server output:\n{recent}"


__all__ = [
    "NatsServerBinaryResolutionError",
    "NatsServerBinaryResolver",
    "NatsServerHandle",
    "NatsServerProcessExited",
    "NatsServerStartupError",
    "NatsServerSupervisor",
    "NatsServerSupervisorError",
    "NatsServerVersionError",
    "ResolvedNatsServerBinary",
    "SupervisedNatsSubstrate",
]
