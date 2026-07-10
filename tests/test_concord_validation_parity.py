from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace

import anyio
import pytest

from deckr._concord._validation import ConcordSessionAssertions
from deckr.concord import (
    CONCORD_CONTRACT_SCHEMA_ID,
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityReason,
    ContractValidityStatus,
    ParticipantTokenRecord,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvEntry, KvViewStatus
from deckr.testing import ConcordRuntimeHarness, MemoryJsonKvBucket

_CONTRACT_ID = "validation-parity-contract"
_GENERATION = 1
_PROFILE = "dev.deckr.test.validation_parity.v1"
_TERMS_HASH = "sha256:validation-parity-terms"
_CONTROLLER = controller_address("controller-main")
_MANAGER = hardware_manager_address("manager-main")
_PARTICIPANTS = tuple(sorted((_CONTROLLER, _MANAGER), key=str))
_CONTROLLER_KEY = str(_CONTROLLER)
_MANAGER_KEY = str(_MANAGER)
_SESSIONS = {
    _CONTROLLER_KEY: "controller-session",
    _MANAGER_KEY: "manager-session",
}


@dataclass(frozen=True, slots=True)
class _ExpectedValidity:
    status: ContractValidityStatus
    reason_code: ContractValidityReason | None
    token_participants: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _ValidationCase:
    harness: ConcordRuntimeHarness
    handle: ContractHandle
    contract: ContractRecord | None
    current_sessions: dict[str, str]


_PARITY_CASES = (
    pytest.param(
        "missing_contract",
        _ExpectedValidity(
            ContractValidityStatus.MISSING_CONTRACT,
            ContractValidityReason.CONTRACT_MISSING,
        ),
        id="missing-contract",
    ),
    pytest.param(
        "malformed_contract",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            ContractValidityReason.CONTRACT_MALFORMED,
        ),
        id="malformed-contract",
    ),
    pytest.param(
        "contract_key_mismatch",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            ContractValidityReason.CONTRACT_KEY_MISMATCH,
        ),
        id="contract-key-mismatch",
    ),
    pytest.param(
        "contract_pointer_mismatch",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            ContractValidityReason.CONTRACT_POINTER_MISMATCH,
        ),
        id="contract-pointer-mismatch",
    ),
    pytest.param(
        "cancelled_contract",
        _ExpectedValidity(
            ContractValidityStatus.CANCELLED,
            ContractValidityReason.CONTRACT_CANCELLED,
        ),
        id="cancelled-contract",
    ),
    pytest.param(
        "pending_without_tokens",
        _ExpectedValidity(
            ContractValidityStatus.NOT_YET_FULFILLED,
            ContractValidityReason.PARTICIPANT_NOT_ATTACHED,
        ),
        id="pending-without-tokens",
    ),
    pytest.param(
        "pending_with_one_token",
        _ExpectedValidity(
            ContractValidityStatus.NOT_YET_FULFILLED,
            ContractValidityReason.PARTICIPANT_NOT_ATTACHED,
            frozenset({_CONTROLLER_KEY}),
        ),
        id="pending-with-one-token",
    ),
    pytest.param(
        "valid_contract",
        _ExpectedValidity(
            ContractValidityStatus.VALID,
            None,
            frozenset({_CONTROLLER_KEY, _MANAGER_KEY}),
        ),
        id="valid-contract",
    ),
    pytest.param(
        "missing_attached_token",
        _ExpectedValidity(
            ContractValidityStatus.MISSING_TOKEN,
            ContractValidityReason.TOKEN_MISSING,
            frozenset({_MANAGER_KEY}),
        ),
        id="missing-attached-token",
    ),
    pytest.param(
        "malformed_token",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityReason.TOKEN_MALFORMED,
            frozenset({_MANAGER_KEY}),
        ),
        id="malformed-token",
    ),
    pytest.param(
        "wrong_token_contract",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityReason.TOKEN_CONTRACT_MISMATCH,
            frozenset({_MANAGER_KEY}),
        ),
        id="wrong-token-contract",
    ),
    pytest.param(
        "wrong_token_generation",
        _ExpectedValidity(
            ContractValidityStatus.GENERATION_MISMATCH,
            ContractValidityReason.TOKEN_GENERATION_MISMATCH,
            frozenset({_MANAGER_KEY}),
        ),
        id="wrong-token-generation",
    ),
    pytest.param(
        "wrong_token_participant",
        _ExpectedValidity(
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityReason.TOKEN_PARTICIPANT_MISMATCH,
            frozenset({_MANAGER_KEY}),
        ),
        id="wrong-token-participant",
    ),
    pytest.param(
        "wrong_token_terms",
        _ExpectedValidity(
            ContractValidityStatus.TERMS_HASH_MISMATCH,
            ContractValidityReason.TOKEN_TERMS_HASH_MISMATCH,
            frozenset({_CONTROLLER_KEY, _MANAGER_KEY}),
        ),
        id="wrong-token-terms",
    ),
    pytest.param(
        "wrong_token_session",
        _ExpectedValidity(
            ContractValidityStatus.SESSION_MISMATCH,
            ContractValidityReason.TOKEN_SESSION_MISMATCH,
            frozenset({_CONTROLLER_KEY, _MANAGER_KEY}),
        ),
        id="wrong-token-session",
    ),
)


@pytest.mark.parametrize(("case_name", "expected"), _PARITY_CASES)
@pytest.mark.asyncio
async def test_exact_and_cached_validation_have_parity(
    case_name: str,
    expected: _ExpectedValidity,
) -> None:
    case = await _build_case(case_name)

    exact = await case.harness.concord.validate_exact(
        case.handle,
        current_sessions=case.current_sessions,
    )
    cached = await case.harness.concord.validate(
        case.handle,
        current_sessions=case.current_sessions,
    )

    assert exact.status == cached.status == expected.status
    assert exact.contract == cached.contract == case.contract
    assert exact.tokens == cached.tokens
    assert frozenset(exact.tokens) == expected.token_participants
    assert exact.reason_code == cached.reason_code == expected.reason_code
    if expected.reason_code is None:
        assert exact.reason is None
        assert cached.reason is None
    else:
        assert exact.reason is not None
        assert cached.reason is not None
        assert exact.reason != expected.reason_code.value
        assert cached.reason != expected.reason_code.value
    if case_name == "cancelled_contract":
        assert exact.reason == ContractValidityReason.TOKEN_MISSING.value
        assert exact.reason_code == ContractValidityReason.CONTRACT_CANCELLED
    _assert_no_runtime_watch_started(case.harness)


@pytest.mark.asyncio
async def test_stale_cached_source_does_not_disable_exact_validation() -> None:
    case = await _build_case("valid_contract")
    await case.harness._contract_view._set_status(KvViewStatus.STALE)  # noqa: SLF001
    await case.harness._token_view._set_status(KvViewStatus.STALE)  # noqa: SLF001
    case.harness.concord._started = True  # noqa: SLF001
    case.harness.concord._ready.set()  # noqa: SLF001

    cached = await case.harness.concord.validate(
        case.handle,
        current_sessions=case.current_sessions,
    )
    exact = await case.harness.concord.validate_exact(
        case.handle,
        current_sessions=case.current_sessions,
    )

    assert cached.status == ContractValidityStatus.UNAVAILABLE
    assert cached.reason_code == ContractValidityReason.SOURCE_UNAVAILABLE
    assert exact.status == ContractValidityStatus.VALID
    assert exact.reason_code is None
    assert exact.contract == case.contract
    assert frozenset(exact.tokens) == {_CONTROLLER_KEY, _MANAGER_KEY}
    _assert_no_runtime_watch_started(case.harness)


@pytest.mark.asyncio
async def test_token_entry_key_mismatch_has_exact_and_cached_parity() -> None:
    case = await _build_case("valid_contract")
    canonical_key = _token_key(_CONTROLLER)
    raw_entry = await case.harness.token_store.get(canonical_key)
    assert raw_entry is not None
    mismatched = KvEntry(
        raw_entry.bucket,
        _token_key(_MANAGER),
        raw_entry.value,
        raw_entry.revision,
    )
    case.harness.token_store._entries[canonical_key] = mismatched  # noqa: SLF001
    case.harness.concord._token_entries_by_key[canonical_key] = mismatched  # noqa: SLF001

    exact = await case.harness.concord.validate_exact(
        case.handle,
        current_sessions=case.current_sessions,
    )
    cached = await case.harness.concord.validate(
        case.handle,
        current_sessions=case.current_sessions,
    )

    assert exact.status == cached.status == ContractValidityStatus.INVALID_TOKEN
    assert exact.reason_code == cached.reason_code == (
        ContractValidityReason.TOKEN_KEY_MISMATCH
    )
    assert exact.contract == cached.contract == case.contract
    assert exact.tokens == cached.tokens
    assert frozenset(exact.tokens) == {_MANAGER_KEY}
    _assert_no_runtime_watch_started(case.harness)


@pytest.mark.asyncio
async def test_contract_entry_key_mismatch_has_exact_and_cached_parity() -> None:
    case = await _build_case("valid_contract")
    canonical_key = _contract_key()
    raw_entry = await case.harness.contract_store.get(canonical_key)
    assert raw_entry is not None
    mismatched = KvEntry(
        raw_entry.bucket,
        concord_contract_key(contract_id="different-contract", generation=1),
        raw_entry.value,
        raw_entry.revision,
    )
    case.harness.contract_store._entries[canonical_key] = mismatched  # noqa: SLF001
    case.harness.concord._contract_entries_by_key[canonical_key] = mismatched  # noqa: SLF001

    exact = await case.harness.concord.validate_exact(
        case.handle,
        current_sessions=case.current_sessions,
    )
    cached = await case.harness.concord.validate(
        case.handle,
        current_sessions=case.current_sessions,
    )

    assert exact.status == cached.status == ContractValidityStatus.INVALID_CONTRACT
    assert exact.reason_code == cached.reason_code == (
        ContractValidityReason.CONTRACT_KEY_MISMATCH
    )
    assert exact.contract == cached.contract == case.contract
    assert exact.tokens == cached.tokens
    _assert_no_runtime_watch_started(case.harness)


def test_session_assertions_copy_normalize_sort_and_freeze_the_input() -> None:
    source = {
        _MANAGER: "manager-session",
        _CONTROLLER_KEY: "controller-session",
    }

    assertions = ConcordSessionAssertions.from_mapping(source)
    source[_CONTROLLER_KEY] = "replacement-session"
    source.clear()

    assert assertions.by_participant == (
        (_CONTROLLER_KEY, "controller-session"),
        (_MANAGER_KEY, "manager-session"),
    )
    assert assertions.get(_CONTROLLER) == "controller-session"
    with pytest.raises(FrozenInstanceError):
        assertions.by_participant = ()


@pytest.mark.asyncio
async def test_exact_validation_freezes_sessions_before_its_first_store_read() -> None:
    contract_store = _PausingExactGetBucket(bucket="contracts")
    harness = ConcordRuntimeHarness(contract_store=contract_store)
    case = await _build_case("valid_contract", harness=harness)
    contract_store.pause_key = case.handle.key
    results: list[ContractValidity] = []

    async def validate() -> None:
        results.append(
            await harness.concord.validate_exact(
                case.handle,
                current_sessions=case.current_sessions,
            )
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(validate)
        await contract_store.read_started.wait()
        case.current_sessions[_CONTROLLER_KEY] = "replacement-session"
        contract_store.resume_read.set()

    assert len(results) == 1
    assert results[0].status == ContractValidityStatus.VALID
    assert results[0].reason_code is None
    _assert_no_runtime_watch_started(harness)


async def _build_case(
    case_name: str,
    *,
    harness: ConcordRuntimeHarness | None = None,
) -> _ValidationCase:
    harness = harness or ConcordRuntimeHarness()
    sessions = dict(_SESSIONS)
    expected_contract: ContractRecord | None

    if case_name == "missing_contract":
        handle = _contract_handle()
        expected_contract = None
    elif case_name == "malformed_contract":
        entry = await harness.seed_raw_contract(
            _contract_key(),
            {
                "schema": CONCORD_CONTRACT_SCHEMA_ID,
                "contractId": _CONTRACT_ID,
                "generation": _GENERATION,
            },
        )
        handle = _contract_handle(revision=entry.revision)
        expected_contract = None
    elif case_name == "contract_key_mismatch":
        expected_contract = _contract_record()
        handle = await harness.seed_contract(expected_contract)
        handle = replace(handle, key="contracts.not-canonical.1.meta")
    elif case_name == "contract_pointer_mismatch":
        expected_contract = _contract_record(contract_id="different-contract")
        entry = await harness.seed_raw_contract(
            _contract_key(),
            expected_contract.to_dict(),
        )
        handle = _contract_handle(revision=entry.revision)
    elif case_name == "cancelled_contract":
        expected_contract = _contract_record(
            state=ContractState.CANCELLED,
            cancel_reason=ContractValidityReason.TOKEN_MISSING.value,
        )
        handle = await harness.seed_contract(expected_contract)
    elif case_name in {"pending_without_tokens", "pending_with_one_token"}:
        expected_contract = _contract_record()
        handle = await harness.seed_contract(expected_contract)
        if case_name == "pending_with_one_token":
            await _seed_token(harness, _CONTROLLER, _token_record(_CONTROLLER))
    else:
        expected_contract = _contract_record(attached=_PARTICIPANTS)
        handle = await harness.seed_contract(expected_contract)
        if case_name == "missing_attached_token":
            await _seed_token(harness, _MANAGER, _token_record(_MANAGER))
        elif case_name == "malformed_token":
            await harness.seed_raw_token(
                _token_key(_CONTROLLER),
                {
                    "schema": CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
                    "contractId": _CONTRACT_ID,
                    "generation": _GENERATION,
                },
            )
            await _seed_token(harness, _MANAGER, _token_record(_MANAGER))
        else:
            controller_token = _token_record(_CONTROLLER)
            if case_name == "wrong_token_contract":
                controller_token = _token_record(
                    _CONTROLLER,
                    contract_id="different-contract",
                )
            elif case_name == "wrong_token_generation":
                controller_token = _token_record(_CONTROLLER, generation=2)
            elif case_name == "wrong_token_participant":
                controller_token = _token_record(
                    _MANAGER,
                    token_id="wrong-participant-token",
                )
            elif case_name == "wrong_token_terms":
                controller_token = _token_record(
                    _CONTROLLER,
                    terms_hash="sha256:different-terms",
                )
            elif case_name == "wrong_token_session":
                sessions[_CONTROLLER_KEY] = "replacement-session"
            await _seed_token(harness, _CONTROLLER, controller_token)
            await _seed_token(harness, _MANAGER, _token_record(_MANAGER))

    await harness.materialize()
    return _ValidationCase(
        harness=harness,
        handle=handle,
        contract=expected_contract,
        current_sessions=sessions,
    )


def _contract_record(
    *,
    contract_id: str = _CONTRACT_ID,
    attached: tuple = (),
    state: ContractState = ContractState.OPEN,
    cancel_reason: str | None = None,
) -> ContractRecord:
    return ContractRecord(
        contractId=contract_id,
        generation=_GENERATION,
        participants=_PARTICIPANTS,
        attachedParticipants=tuple(sorted(attached, key=str)),
        state=state,
        profile=_PROFILE,
        termsHash=_TERMS_HASH,
        cancelReason=cancel_reason,
    )


def _token_record(
    participant,
    *,
    contract_id: str = _CONTRACT_ID,
    generation: int = _GENERATION,
    session_id: str | None = None,
    token_id: str | None = None,
    terms_hash: str = _TERMS_HASH,
) -> ParticipantTokenRecord:
    participant_key = str(participant)
    return ParticipantTokenRecord(
        contractId=contract_id,
        generation=generation,
        participant=participant,
        sessionId=session_id or _SESSIONS[participant_key],
        tokenId=token_id or f"token-{participant_key}",
        refreshSeq=1,
        ttlSeconds=120,
        termsHash=terms_hash,
    )


def _contract_handle(*, revision: int = 1) -> ContractHandle:
    return ContractHandle(
        key=_contract_key(),
        contract_id=_CONTRACT_ID,
        generation=_GENERATION,
        participants=_PARTICIPANTS,
        attached_participants=(),
        revision=revision,
        state=ContractState.OPEN,
        profile=_PROFILE,
        terms_hash=_TERMS_HASH,
    )


async def _seed_token(
    harness: ConcordRuntimeHarness,
    expected_participant,
    record: ParticipantTokenRecord,
) -> None:
    await harness.seed_raw_token(
        _token_key(expected_participant),
        record.to_dict(),
    )


def _contract_key() -> str:
    return concord_contract_key(
        contract_id=_CONTRACT_ID,
        generation=_GENERATION,
    )


def _token_key(participant) -> str:
    return concord_participant_token_key(
        contract_id=_CONTRACT_ID,
        generation=_GENERATION,
        participant=participant,
    )


def _assert_no_runtime_watch_started(harness: ConcordRuntimeHarness) -> None:
    for store in (harness.contract_store, harness.token_store):
        assert store.start_count == 0
        assert store.watch_count == 0
        assert store.active_watch_count == 0
        assert store.subscription_count == 0
        assert store.active_subscription_count == 0


class _PausingExactGetBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket)
        self.pause_key: str | None = None
        self.read_started = anyio.Event()
        self.resume_read = anyio.Event()
        self._paused = False

    async def get(self, key: str):
        if key == self.pause_key and not self._paused:
            self._paused = True
            self.read_started.set()
            await self.resume_read.wait()
        return await super().get(key)
