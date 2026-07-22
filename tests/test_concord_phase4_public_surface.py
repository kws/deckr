from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from dataclasses import FrozenInstanceError, fields
from inspect import iscoroutinefunction, signature
from pathlib import Path

import pytest

import deckr.beacon as beacon_module
import deckr.concord as concord_module
from deckr.beacon import Beacon, BeaconAdvertisementLease, BeaconAdvertisementSpec
from deckr.concord import (
    Concord,
    ConcordAgreementLease,
    ConcordAgreementSpec,
    ConcordAgreementState,
    ConcordLifecyclePhase,
    ConcordParticipant,
    ConcordParticipantState,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    concord_contract_key,
)
from deckr.contracts.messages import controller_address
from deckr.services import (
    DeckrServices,
    ManagedService,
    ManagedServiceFailure,
    ManagedServicePhase,
    ManagedServiceState,
    ServiceProvider,
    SharedResourceSubscriptionManager,
)

_PARTICIPANT = controller_address("test")


def _agreement_contract() -> tuple[ContractHandle, ContractRecord]:
    participants = (_PARTICIPANT,)
    contract = ContractHandle(
        key=concord_contract_key(contract_id="test", generation=1),
        contract_id="test",
        generation=1,
        participants=participants,
        attached_participants=participants,
        revision=1,
        state=ContractState.OPEN,
    )
    record = ContractRecord(
        contract_id=contract.contract_id,
        generation=contract.generation,
        participants=participants,
        attached_participants=participants,
        state=contract.state,
    )
    return contract, record


def test_beacon_advertisement_factory_is_endpoint_owned_and_intent_only() -> None:
    assert tuple(signature(Beacon.advertise).parameters) == (
        "self",
        "endpoint",
        "spec",
    )
    assert not iscoroutinefunction(Beacon.advertise)
    assert tuple(field.name for field in fields(BeaconAdvertisementSpec)) == (
        "feature_id",
        "protocol",
        "operations",
        "labels",
        "hints",
        "payload",
    )
    assert "AdvertisementHandle" not in beacon_module.__all__


def test_beacon_advertisement_spec_is_deeply_immutable() -> None:
    labels = {"room": "office"}
    payload = {"nested": {"ready": True}}
    spec = BeaconAdvertisementSpec(
        feature_id="dev.deckr.test.feature",
        labels=labels,
        payload=payload,
    )

    labels["room"] = "kitchen"
    payload["nested"]["ready"] = False

    assert spec.labels == {"room": "office"}
    assert spec.payload == {"nested": {"ready": True}}
    with pytest.raises(TypeError):
        spec.labels["room"] = "garage"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.payload["nested"]["ready"] = False  # type: ignore[index]


def test_beacon_advertisement_context_hides_manual_lifecycle_and_raw_handle() -> None:
    for name in ("handle", "start", "start_soon", "withdraw"):
        assert not hasattr(BeaconAdvertisementLease, name)


def test_managed_service_capability_owns_operations_without_raw_lease_plumbing() -> None:
    for name in ("contract", "agreement", "refresh"):
        assert not hasattr(ManagedService, name)
    for name in (
        "send",
        "request",
        "read_view",
        "watch_view",
        "view_access",
        "authorize_inbound_message",
    ):
        assert hasattr(ManagedService, name)
        assert not hasattr(DeckrServices, name)
    parameters = signature(SharedResourceSubscriptionManager).parameters
    assert "reconnect_delay_seconds" not in parameters
    assert "lease_monitor_interval_seconds" not in parameters


def test_managed_service_state_is_immutable_versioned_and_opaque() -> None:
    state = ManagedServiceState(
        version=1,
        phase=ManagedServicePhase.UNAVAILABLE,
        descriptor=None,
        local_authority=False,
        failure=ManagedServiceFailure("unavailable", "not ready"),
    )

    assert tuple(field.name for field in fields(ManagedServiceState)) == (
        "version",
        "phase",
        "descriptor",
        "local_authority",
        "failure",
    )
    assert not hasattr(state, "contract")
    assert not hasattr(state, "token")
    with pytest.raises(FrozenInstanceError):
        state.version = 2  # type: ignore[misc]


def test_managed_service_matching_has_no_single_descriptor_selector() -> None:
    parameters = signature(DeckrServices.use_matching).parameters
    assert tuple(parameters) == (
        "self",
        "protocol",
        "predicate",
        "timeout_seconds",
        "diagnostics",
    )
    assert "select" not in parameters


def test_service_provider_hides_participant_and_manual_lifecycle() -> None:
    for name in ("participant", "start", "start_soon", "reconcile", "refresh"):
        assert not hasattr(ServiceProvider, name)
    for name in ("state", "watch_state", "wait_for_state", "requests", "authorize"):
        assert hasattr(ServiceProvider, name)


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
    assert not hasattr(concord_module, "ParticipantHandle")


def test_agreement_spec_contains_only_intent_and_static_assertions() -> None:
    assert tuple(field.name for field in fields(ConcordAgreementSpec)) == (
        "profile",
        "participants",
        "terms",
        "supersedes",
        "participant_sessions",
        "created_by",
    )


def test_agreement_spec_is_deeply_immutable() -> None:
    terms = {"nested": {"enabled": True}}
    sessions = {"controller:test": "session-1"}
    spec = ConcordAgreementSpec(
        profile="dev.deckr.test.v1",
        participants=("controller:test",),
        terms=terms,
        participant_sessions=sessions,
    )

    terms["nested"]["enabled"] = False
    sessions["controller:test"] = "session-2"

    assert spec.terms == {"nested": {"enabled": True}}
    assert spec.participant_sessions == {"controller:test": "session-1"}
    with pytest.raises(TypeError):
        spec.terms["nested"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.participant_sessions["controller:test"] = "session-2"  # type: ignore[index]


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


@pytest.mark.parametrize("phase", tuple(ConcordLifecyclePhase))
def test_agreement_validity_requires_ready_current_local_authority(
    phase: ConcordLifecyclePhase,
) -> None:
    contract, record = _agreement_contract()
    state = ConcordAgreementState(
        version=1,
        phase=phase,
        current=True,
        contract=contract,
        validity=ContractValidity(
            ContractValidityStatus.VALID,
            contract=record,
        ),
        local_authority=True,
    )

    assert state.valid is (phase is ConcordLifecyclePhase.READY)


def test_agreement_cannot_be_valid_without_a_contract() -> None:
    _contract, record = _agreement_contract()
    state = ConcordAgreementState(
        version=1,
        phase=ConcordLifecyclePhase.READY,
        current=True,
        contract=None,
        validity=ContractValidity(
            ContractValidityStatus.VALID,
            contract=record,
        ),
        local_authority=True,
    )

    assert not state.valid


@pytest.mark.parametrize(
    ("current", "local_authority", "status"),
    (
        (False, True, ContractValidityStatus.VALID),
        (True, False, ContractValidityStatus.VALID),
        (True, True, ContractValidityStatus.NOT_YET_FULFILLED),
    ),
)
def test_agreement_validity_rejects_each_missing_authority_requirement(
    current: bool,
    local_authority: bool,
    status: ContractValidityStatus,
) -> None:
    contract, record = _agreement_contract()
    state = ConcordAgreementState(
        version=1,
        phase=ConcordLifecyclePhase.READY,
        current=current,
        contract=contract,
        validity=ContractValidity(status, contract=record),
        local_authority=local_authority,
    )

    assert not state.valid


def test_agreement_validity_requires_matching_materialized_contract() -> None:
    contract, _record = _agreement_contract()
    mismatched = ContractRecord(
        contract_id="other",
        generation=1,
        participants=(_PARTICIPANT,),
        attached_participants=(_PARTICIPANT,),
        state=ContractState.OPEN,
    )
    state = ConcordAgreementState(
        version=1,
        phase=ConcordLifecyclePhase.READY,
        current=True,
        contract=contract,
        validity=ContractValidity(
            ContractValidityStatus.VALID,
            contract=mismatched,
        ),
        local_authority=True,
    )

    assert not state.valid


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
        "assert not hasattr(concord, 'ParticipantHandle'); "
        "import deckr.beacon as beacon; "
        "assert not hasattr(beacon, 'AdvertisementHandle'); "
        "import deckr.services as services; "
        "assert hasattr(services, 'ManagedService'); "
        "assert not hasattr(services, 'ServiceUseLease'); "
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
