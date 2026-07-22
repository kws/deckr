from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import product


class _Event(StrEnum):
    PROPOSE = "propose"
    TOKEN_COMMITTED = "token_committed"
    ATTACHED = "attached"
    AUTHORITY_LOST = "authority_lost"
    CANCELLED = "cancelled"
    CLEANED = "cleaned"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _AuthorityModel:
    phase: str = "starting"
    proposal_count: int = 0
    contract_ids: tuple[str, ...] = ()
    partial: bool = False
    authoritative: bool = False
    retained_cleanup: bool = False
    previously_attached_or_lost: bool = False


def _transition(state: _AuthorityModel, event: _Event) -> _AuthorityModel:
    if state.phase in {"cancelled", "closed"}:
        return state
    if event == _Event.PROPOSE:
        if state.retained_cleanup:
            return state
        sequence = state.proposal_count + 1
        return replace(
            state,
            phase="connecting",
            proposal_count=sequence,
            contract_ids=(*state.contract_ids, f"contract-{sequence}"),
            partial=False,
            authoritative=False,
            retained_cleanup=False,
            previously_attached_or_lost=False,
        )
    if (
        event == _Event.TOKEN_COMMITTED
        and state.contract_ids
        and state.phase == "connecting"
        and not state.previously_attached_or_lost
    ):
        return replace(state, partial=True, retained_cleanup=True)
    if (
        event == _Event.ATTACHED
        and state.partial
        and not state.previously_attached_or_lost
    ):
        return replace(
            state,
            phase="ready",
            partial=False,
            authoritative=True,
            retained_cleanup=False,
            previously_attached_or_lost=True,
        )
    if event == _Event.AUTHORITY_LOST:
        return replace(
            state,
            phase="unavailable",
            authoritative=False,
            retained_cleanup=state.partial or state.authoritative,
            previously_attached_or_lost=True,
        )
    if event == _Event.CANCELLED:
        return replace(state, phase="cancelled", authoritative=False)
    if event == _Event.CLEANED:
        return replace(state, partial=False, retained_cleanup=False)
    if event == _Event.CLOSED:
        return replace(state, phase="closed", authoritative=False)
    return state


def test_authority_transition_model_properties_are_exhaustive() -> None:
    events = tuple(_Event)
    for sequence in product(events, repeat=5):
        state = _AuthorityModel()
        for event in sequence:
            before = state
            state = _transition(state, event)

            assert len(set(state.contract_ids)) == len(state.contract_ids)
            if state.phase == "ready":
                assert state.authoritative
                assert not state.partial
                assert state.contract_ids
            if state.partial:
                assert state.retained_cleanup
            if before.previously_attached_or_lost and event == _Event.ATTACHED:
                assert not state.authoritative or before.authoritative
            if before.phase in {"cancelled", "closed"}:
                assert state == before


def test_cancelled_and_closed_transition_states_are_absorbing() -> None:
    for phase in ("cancelled", "closed"):
        state = _AuthorityModel(phase=phase, authoritative=False)
        for event in _Event:
            assert _transition(state, event) == state
