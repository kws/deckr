from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from deckr._authority_buckets import (
    RESERVED_AUTHORITY_BUCKET_POLICIES,
    ConcordMaintenanceStores,
)
from deckr.beacon import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
)
from deckr.components import ComponentContext, ComponentManifest, LaneRegistry
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
)
from deckr.concord_maintenance import (
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
)
from deckr.runtime import Deckr
from deckr.substrates.nats_kv import KvBucketPolicy

_AUTHORITY_POLICY_NAMES = {
    "BEACON_ADVERTISEMENT_STORE_POLICY",
    "CONCORD_CONTRACT_BUCKET_POLICY",
    "CONCORD_MAINTENANCE_BUCKET_POLICY",
    "CONCORD_TOKEN_BUCKET_POLICY",
}
_RESERVED_BUCKET_NAMES = frozenset(RESERVED_AUTHORITY_BUCKET_POLICIES)

_CallSite = tuple[Path, str, str, str]

_RESERVED_POLICY_CASES = tuple(RESERVED_AUTHORITY_BUCKET_POLICIES.values()) + tuple(
    KvBucketPolicy(
        bucket=policy.bucket,
        ttl_seconds=1,
        description="forged authority policy",
    )
    for policy in RESERVED_AUTHORITY_BUCKET_POLICIES.values()
)


class _RecordingBucketOpener:
    def __init__(self) -> None:
        self.calls: list[KvBucketPolicy] = []

    def kv_bucket(self, policy: KvBucketPolicy) -> KvBucketPolicy:
        self.calls.append(policy)
        return policy


def _component_context(
    *,
    kv_bucket_for=None,
    concord_maintenance_stores_for=None,
) -> ComponentContext:
    return ComponentContext(
        component_id="dev.deckr.test",
        instance_id="main",
        runtime_name="dev.deckr.test:main",
        manifest=ComponentManifest(component_id="dev.deckr.test"),
        config={},
        endpoints={},
        base_dir=Path.cwd(),
        lanes=LaneRegistry({}),
        kv_bucket_for=kv_bucket_for,
        _concord_maintenance_stores_for=concord_maintenance_stores_for,
    )


def test_public_authority_bucket_constants_reexport_internal_registry() -> None:
    expected = {
        DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME: BEACON_ADVERTISEMENT_STORE_POLICY,
        DEFAULT_CONCORD_CONTRACT_BUCKET_NAME: CONCORD_CONTRACT_BUCKET_POLICY,
        DEFAULT_CONCORD_TOKEN_BUCKET_NAME: CONCORD_TOKEN_BUCKET_POLICY,
        DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME: CONCORD_MAINTENANCE_BUCKET_POLICY,
    }

    assert dict(RESERVED_AUTHORITY_BUCKET_POLICIES) == expected
    for bucket, policy in expected.items():
        assert RESERVED_AUTHORITY_BUCKET_POLICIES[bucket] is policy


def test_generic_component_kv_cannot_open_reserved_authority_buckets() -> None:
    workspace = Path(__file__).resolve().parents[2]
    production_files = _production_python_files(workspace)
    observed: Counter[_CallSite] = Counter()
    for path in production_files:
        relative = path.relative_to(workspace)
        tree = ast.parse(path.read_text(), filename=str(path))
        visitor = _GenericAuthorityKvVisitor(relative)
        visitor.visit(tree)
        observed.update(visitor.calls)

    assert not observed, "unexpected generic authority bucket access:\n" + "\n".join(
        _format_callsites(observed)
    )


@pytest.mark.parametrize("policy", _RESERVED_POLICY_CASES)
def test_deckr_generic_kv_rejects_reserved_authority_name(
    policy: KvBucketPolicy,
) -> None:
    opener = _RecordingBucketOpener()
    deckr = Deckr(message_bus=opener)

    with pytest.raises(ValueError, match="reserved"):
        deckr.kv_bucket(policy)

    assert opener.calls == []


@pytest.mark.parametrize("policy", _RESERVED_POLICY_CASES)
def test_component_generic_kv_rejects_reserved_authority_name(
    policy: KvBucketPolicy,
) -> None:
    opener = _RecordingBucketOpener()
    context = _component_context(kv_bucket_for=opener.kv_bucket)

    with pytest.raises(ValueError, match="reserved"):
        context.kv_bucket(policy)

    assert opener.calls == []


def test_component_typed_maintenance_route_uses_canonical_store_bundle() -> None:
    opener = _RecordingBucketOpener()
    deckr = Deckr(message_bus=opener)
    context = _component_context(
        concord_maintenance_stores_for=deckr._concord_maintenance_stores,  # noqa: SLF001
    )

    stores = context._concord_maintenance_stores()  # noqa: SLF001

    assert isinstance(stores, ConcordMaintenanceStores)
    assert opener.calls == [
        CONCORD_CONTRACT_BUCKET_POLICY,
        CONCORD_TOKEN_BUCKET_POLICY,
        CONCORD_MAINTENANCE_BUCKET_POLICY,
    ]
    assert stores.contract_store is CONCORD_CONTRACT_BUCKET_POLICY
    assert stores.token_store is CONCORD_TOKEN_BUCKET_POLICY
    assert stores.maintenance_store is CONCORD_MAINTENANCE_BUCKET_POLICY


def test_reserved_authority_bucket_names_have_one_production_definition() -> None:
    workspace = Path(__file__).resolve().parents[2]
    registry = Path("deckr/src/deckr/_authority_buckets.py")
    duplicates: list[str] = []
    for path in _production_python_files(workspace):
        relative = path.relative_to(workspace)
        if relative == registry:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in _RESERVED_BUCKET_NAMES:
                duplicates.append(f"{relative}:{node.lineno}: {node.value}")
    assert not duplicates, "reserved authority bucket name duplicated:\n" + "\n".join(
        f"  {item}" for item in duplicates
    )


class _GenericAuthorityKvVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._scope: list[str] = []
        self._aliases: dict[str, str] = {}
        self.calls: Counter[_CallSite] = Counter()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            if item.name in _AUTHORITY_POLICY_NAMES:
                self._aliases[item.asname or item.name] = item.name

    def visit_Assign(self, node: ast.Assign) -> None:
        policy = self._authority_policy(node.value)
        if policy is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._aliases[target.id] = policy
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            policy = self._authority_policy(node.value)
            if policy is not None and isinstance(node.target, ast.Name):
                self._aliases[node.target.id] = policy
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"kv_bucket", "kv_bucket_for"}
            and node.args
        ):
            policy = self._authority_policy(node.args[0])
            if policy is not None:
                scope = ".".join(self._scope) if self._scope else "<module>"
                self.calls[
                    (self._path, scope, _receiver_name(node.func.value), policy)
                ] += 1
        self.generic_visit(node)

    def _visit_callable(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _authority_policy(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._aliases.get(
                node.id,
                node.id if node.id in _AUTHORITY_POLICY_NAMES else None,
            )
        if isinstance(node, ast.Attribute) and node.attr in _AUTHORITY_POLICY_NAMES:
            return node.attr
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and child.value in _RESERVED_BUCKET_NAMES
            ):
                return str(child.value)
        return None


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _receiver_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _production_python_files(workspace: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for child in workspace.iterdir():
        src = child / "src"
        if not child.name.startswith("deckr") or not src.is_dir():
            continue
        files.extend(
            path
            for path in src.rglob("*.py")
            if "tests" not in path.parts and "__pycache__" not in path.parts
        )
    return tuple(sorted(files))


def _format_callsites(calls: Counter[_CallSite]) -> list[str]:
    return [
        f"  {path}:{scope}: {receiver}.kv_bucket({policy}) x{count}"
        for (path, scope, receiver, policy), count in sorted(calls.items())
    ]
