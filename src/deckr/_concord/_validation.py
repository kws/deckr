from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from deckr._concord._keys import (
    concord_contract_key,
    concord_participant_token_key,
)
from deckr._concord._models import (
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityReason,
    ContractValidityStatus,
    ParticipantHandle,
    ParticipantTokenRecord,
    participant_handle,
    require_text,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.substrates.nats_kv import KvEntry


class ConcordObservationState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ContractObservation:
    key: str
    revision: int | None
    state: ConcordObservationState
    record: ContractRecord | None = None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class ParticipantTokenObservation:
    key: str
    revision: int | None
    expected_participant: EndpointAddress
    state: ConcordObservationState
    record: ParticipantTokenRecord | None = None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class ConcordSessionAssertions:
    by_participant: tuple[tuple[str, str], ...]

    def __init__(
        self,
        by_participant: (
            Mapping[str | EndpointAddress, str]
            | Iterable[tuple[str | EndpointAddress, str]]
            | None
        ) = None,
    ) -> None:
        items = (
            ()
            if by_participant is None
            else by_participant.items()
            if isinstance(by_participant, Mapping)
            else by_participant
        )
        normalized: dict[str, str] = {}
        for participant, session_id in items:
            participant_key = str(parse_endpoint_address(participant))
            session = require_text(session_id, field_name="Concord session assertion")
            existing = normalized.get(participant_key)
            if existing is not None and existing != session:
                raise ValueError(
                    "Concord session assertions contain conflicting normalized participants"
                )
            normalized[participant_key] = session
        object.__setattr__(
            self,
            "by_participant",
            tuple(sorted(normalized.items())),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str | EndpointAddress, str] | None,
    ) -> ConcordSessionAssertions:
        return cls(value)

    def get(self, participant: str | EndpointAddress) -> str | None:
        key = str(parse_endpoint_address(participant))
        return dict(self.by_participant).get(key)


def contract_observation_from_entry(
    key: str,
    entry: KvEntry | None,
) -> ContractObservation:
    if entry is None:
        return ContractObservation(key, None, ConcordObservationState.MISSING)
    try:
        record = ContractRecord.model_validate(entry.value)
    except (TypeError, ValueError) as exc:
        return ContractObservation(
            entry.key,
            entry.revision,
            ConcordObservationState.MALFORMED,
            diagnostic=str(exc),
        )
    return ContractObservation(
        entry.key,
        entry.revision,
        ConcordObservationState.PRESENT,
        record=record,
    )


def token_observation_from_entry(
    key: str,
    expected_participant: str | EndpointAddress,
    entry: KvEntry | None,
) -> ParticipantTokenObservation:
    participant = parse_endpoint_address(expected_participant)
    if entry is None:
        return ParticipantTokenObservation(
            key,
            None,
            participant,
            ConcordObservationState.MISSING,
        )
    try:
        record = ParticipantTokenRecord.model_validate(entry.value)
    except (TypeError, ValueError) as exc:
        return ParticipantTokenObservation(
            entry.key,
            entry.revision,
            participant,
            ConcordObservationState.MALFORMED,
            diagnostic=str(exc),
        )
    return ParticipantTokenObservation(
        entry.key,
        entry.revision,
        participant,
        ConcordObservationState.PRESENT,
        record=record,
    )


def evaluate_contract_validity(
    *,
    expected_key: str,
    expected_pointer: ContractPointer,
    contract: ContractObservation,
    tokens: tuple[ParticipantTokenObservation, ...],
    session_assertions: ConcordSessionAssertions | None = None,
) -> ContractValidity:
    """Evaluate immutable Concord observations without performing I/O."""

    assertions = session_assertions or ConcordSessionAssertions()
    parsed_contract = contract.record
    parsed_tokens = _identity_consistent_token_handles(
        parsed_contract,
        tokens,
    )

    unavailable = _first_unavailable_observation(contract, tokens)
    if unavailable is not None:
        return ContractValidity(
            ContractValidityStatus.UNAVAILABLE,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason=unavailable,
            reason_code=ContractValidityReason.SOURCE_UNAVAILABLE,
        )
    if contract.state == ConcordObservationState.MISSING:
        return ContractValidity(
            ContractValidityStatus.MISSING_CONTRACT,
            reason=contract.diagnostic or contract.key,
            reason_code=ContractValidityReason.CONTRACT_MISSING,
        )
    if contract.state == ConcordObservationState.MALFORMED or parsed_contract is None:
        return ContractValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            reason=contract.diagnostic or contract.key,
            reason_code=ContractValidityReason.CONTRACT_MALFORMED,
        )

    canonical_expected_key = concord_contract_key(
        contract_id=expected_pointer.contract_id,
        generation=expected_pointer.generation,
    )
    if expected_key != canonical_expected_key:
        return ContractValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason=(
                f"expected contract key {expected_key!r} is not canonical for "
                f"{expected_pointer.contract_id!r} generation "
                f"{expected_pointer.generation}"
            ),
            reason_code=ContractValidityReason.CONTRACT_KEY_MISMATCH,
        )
    if contract.key != canonical_expected_key:
        return ContractValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason=(
                f"observed contract key {contract.key!r} differs from expected "
                f"key {canonical_expected_key!r}"
            ),
            reason_code=ContractValidityReason.CONTRACT_KEY_MISMATCH,
        )
    if (
        parsed_contract.contract_id != expected_pointer.contract_id
        or parsed_contract.generation != expected_pointer.generation
    ):
        return ContractValidity(
            ContractValidityStatus.INVALID_CONTRACT,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason="contract record identity differs from the expected pointer",
            reason_code=ContractValidityReason.CONTRACT_POINTER_MISMATCH,
        )
    if parsed_contract.state == ContractState.CANCELLED:
        return ContractValidity(
            ContractValidityStatus.CANCELLED,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason=parsed_contract.cancel_reason or "contract is cancelled",
            reason_code=ContractValidityReason.CONTRACT_CANCELLED,
        )

    observations = {str(item.expected_participant): item for item in tokens}
    attached = {str(item) for item in parsed_contract.attached_participants}
    pending_participant: str | None = None
    for participant in sorted(parsed_contract.participants, key=str):
        participant_key = str(participant)
        expected_token_key = concord_participant_token_key(
            contract_id=expected_pointer.contract_id,
            generation=expected_pointer.generation,
            participant=participant,
        )
        observation = observations.get(participant_key)
        if observation is None or observation.state == ConcordObservationState.MISSING:
            if participant_key in attached:
                return ContractValidity(
                    ContractValidityStatus.MISSING_TOKEN,
                    contract=parsed_contract,
                    tokens=parsed_tokens,
                    reason=participant_key,
                    reason_code=ContractValidityReason.TOKEN_MISSING,
                )
            pending_participant = pending_participant or participant_key
            continue
        if (
            observation.state == ConcordObservationState.MALFORMED
            or observation.record is None
        ):
            return ContractValidity(
                ContractValidityStatus.INVALID_TOKEN,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=observation.diagnostic or observation.key,
                reason_code=ContractValidityReason.TOKEN_MALFORMED,
            )
        token = observation.record
        if observation.key != expected_token_key:
            return ContractValidity(
                ContractValidityStatus.INVALID_TOKEN,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=(
                    f"observed token key {observation.key!r} differs from expected "
                    f"key {expected_token_key!r}"
                ),
                reason_code=ContractValidityReason.TOKEN_KEY_MISMATCH,
            )
        if token.contract_id != expected_pointer.contract_id:
            return ContractValidity(
                ContractValidityStatus.INVALID_TOKEN,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=participant_key,
                reason_code=ContractValidityReason.TOKEN_CONTRACT_MISMATCH,
            )
        if token.generation != expected_pointer.generation:
            return ContractValidity(
                ContractValidityStatus.GENERATION_MISMATCH,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=participant_key,
                reason_code=ContractValidityReason.TOKEN_GENERATION_MISMATCH,
            )
        if token.participant != participant:
            return ContractValidity(
                ContractValidityStatus.INVALID_TOKEN,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=participant_key,
                reason_code=ContractValidityReason.TOKEN_PARTICIPANT_MISMATCH,
            )
        if token.terms_hash != parsed_contract.terms_hash:
            return ContractValidity(
                ContractValidityStatus.TERMS_HASH_MISMATCH,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=participant_key,
                reason_code=ContractValidityReason.TOKEN_TERMS_HASH_MISMATCH,
            )
        asserted_session = assertions.get(participant)
        if asserted_session is not None and token.session_id != asserted_session:
            return ContractValidity(
                ContractValidityStatus.SESSION_MISMATCH,
                contract=parsed_contract,
                tokens=parsed_tokens,
                reason=participant_key,
                reason_code=ContractValidityReason.TOKEN_SESSION_MISMATCH,
            )
        if participant_key not in attached:
            pending_participant = pending_participant or participant_key

    if pending_participant is not None:
        return ContractValidity(
            ContractValidityStatus.NOT_YET_FULFILLED,
            contract=parsed_contract,
            tokens=parsed_tokens,
            reason=pending_participant,
            reason_code=ContractValidityReason.PARTICIPANT_NOT_ATTACHED,
        )
    return ContractValidity(
        ContractValidityStatus.VALID,
        contract=parsed_contract,
        tokens=parsed_tokens,
    )


def _identity_consistent_token_handles(
    contract: ContractRecord | None,
    observations: tuple[ParticipantTokenObservation, ...],
) -> dict[str, ParticipantHandle]:
    if contract is None:
        return {}
    handles: dict[str, ParticipantHandle] = {}
    named = {str(item) for item in contract.participants}
    for observation in observations:
        participant_key = str(observation.expected_participant)
        record = observation.record
        if (
            participant_key not in named
            or observation.state != ConcordObservationState.PRESENT
            or record is None
            or observation.revision is None
        ):
            continue
        expected_key = concord_participant_token_key(
            contract_id=contract.contract_id,
            generation=contract.generation,
            participant=observation.expected_participant,
        )
        if (
            observation.key != expected_key
            or record.contract_id != contract.contract_id
            or record.generation != contract.generation
            or record.participant != observation.expected_participant
        ):
            continue
        handles[participant_key] = participant_handle(
            observation.key,
            record,
            observation.revision,
        )
    return handles


def _first_unavailable_observation(
    contract: ContractObservation,
    tokens: tuple[ParticipantTokenObservation, ...],
) -> str | None:
    if contract.state == ConcordObservationState.UNAVAILABLE:
        return contract.diagnostic or contract.key
    for observation in sorted(tokens, key=lambda item: str(item.expected_participant)):
        if observation.state == ConcordObservationState.UNAVAILABLE:
            return observation.diagnostic or observation.key
    return None
