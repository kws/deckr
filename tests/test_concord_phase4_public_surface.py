from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from dataclasses import FrozenInstanceError, fields
from inspect import signature
from pathlib import Path

import pytest

from deckr.concord import (
    Concord,
    ConcordAgreementLease,
    ConcordAgreementSpec,
    ConcordAgreementState,
    ConcordLifecyclePhase,
    ConcordParticipant,
    ConcordParticipantState,
)


def test_phase4_factories_require_endpoint_owned_contexts() -> None:
    assert tuple(signature(Concord.propose).parameters) == (
        "self",
        "endpoint",
        "spec",
    )
    assert tuple(signature(Concord.participant).parameters) == (
        "self",
        "endpoint",
        "policy",
    )
    assert not hasattr(Concord, "attach")


def test_agreement_spec_contains_only_intent_and_static_assertions() -> None:
    assert tuple(field.name for field in fields(ConcordAgreementSpec)) == (
        "profile",
        "participants",
        "terms",
        "supersedes",
        "current_sessions",
        "created_by",
    )


@pytest.mark.parametrize(
    ("owner", "removed"),
    (
        (
            ConcordAgreementLease,
            ("start", "start_soon", "refresh", "reconcile", "local_token"),
        ),
        (
            ConcordParticipant,
            ("start", "start_soon", "refresh", "reconcile", "release", "watch"),
        ),
    ),
)
def test_phase4_contexts_do_not_expose_caller_owned_lifecycle(
    owner: type,
    removed: tuple[str, ...],
) -> None:
    for name in removed:
        assert not hasattr(owner, name)


def test_phase4_states_are_frozen_and_versioned() -> None:
    agreement = ConcordAgreementState(
        version=1,
        phase=ConcordLifecyclePhase.STARTING,
        current=False,
        contract=None,
        validity=None,
        local_authority=False,
    )
    participant = ConcordParticipantState(
        version=1,
        phase=ConcordLifecyclePhase.STARTING,
        current=False,
        contracts=(),
    )

    with pytest.raises(FrozenInstanceError):
        agreement.version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        participant.version = 2  # type: ignore[misc]


def test_built_wheel_exports_phase4_surface(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(tmp_path)),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("deckr-*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    names = (
        "ConcordAgreementSpec",
        "ConcordAgreementState",
        "ConcordLifecyclePhase",
        "ConcordParticipantPolicy",
        "ConcordParticipantSelection",
        "ConcordParticipantState",
    )
    script = (
        "import sys; "
        f"sys.path[:] = [{str(installed)!r}] + "
        "[path for path in sys.path if "
        "'/work/sidepanel/' not in path or 'site-packages' in path]; "
        "import deckr.concord as concord; "
        f"assert all(hasattr(concord, name) for name in {names!r}); "
        f"assert concord.__file__.startswith({str(installed)!r})"
    )
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith(("COVERAGE", "COV_CORE")):
            environment.pop(name)
    subprocess.run(
        (sys.executable, "-c", script),
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
