from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio

import deckr.hardware.messages as hw_messages
from deckr.beacon import (
    AdvertisementHandle,
    Beacon,
    BeaconAdvertisementLease,
    BeaconAdvertisementSpec,
)
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
    ConcordConflict,
    ConcordParticipant,
    ConcordUnavailable,
    ContractHandle,
    ContractState,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import (
    HARDWARE_MESSAGES_LANE,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
)
from deckr.contracts.models import JsonObject, thaw_json
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimTerms,
    ProfileCapacity,
)
from deckr.substrates.nats_kv import KvConflict, KvUnavailable

logger = logging.getLogger(__name__)

DEFAULT_HARDWARE_ADVERTISEMENT_REFRESH_SECONDS = 5.0
DEFAULT_HARDWARE_CLAIM_RECONCILE_SECONDS = 15.0
DEFAULT_HARDWARE_TOKEN_REFRESH_SECONDS = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
DEFAULT_HARDWARE_WATCH_RETRY_SECONDS = 1.0

_HardwareCommandHandler = Callable[[DeckrMessage], Awaitable[bool | None]]
_HardwareResetHandler = Callable[[str], Awaitable[None]]


class _HardwareEndpoint(Protocol):
    address: EndpointAddress
    session_id: str

    async def send(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress,
        recipient_session_id: str | None = None,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        causation_id: str | None = None,
    ) -> DeckrMessage: ...

    async def reply_to(
        self,
        request: DeckrMessage,
        *,
        message_type: str,
        body: Mapping[str, Any],
        subject: EntitySubject | None = None,
        causation_id: str | None = None,
    ) -> DeckrMessage: ...

    def subscribe(
        self,
        lane: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]: ...


@dataclass(frozen=True, slots=True)
class LiveHardwareClaim:
    contract: ContractHandle
    terms: HardwareClaimTerms
    manager_token: ParticipantHandle
    controller_endpoint: EndpointAddress
    controller_session_id: str
    device_refs: tuple[DeviceRef, ...]

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(ref.device_id for ref in self.device_refs)


@dataclass(slots=True)
class _ClaimCandidate:
    contract: ContractHandle
    terms: HardwareClaimTerms
    token: ParticipantHandle | None
    valid: bool = False
    controller_session_id: str | None = None

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(device.device_ref.device_id for device in self.terms.devices)


@dataclass(slots=True)
class HardwareManagerRuntime:
    endpoint: _HardwareEndpoint
    beacon: Beacon
    concord: Concord
    manager_id: str
    labels: Mapping[str, str] | None = None
    command_handler: _HardwareCommandHandler | None = None
    reset_handler: _HardwareResetHandler | None = None
    advertisement_refresh_seconds: float = (
        DEFAULT_HARDWARE_ADVERTISEMENT_REFRESH_SECONDS
    )
    claim_reconcile_seconds: float = DEFAULT_HARDWARE_CLAIM_RECONCILE_SECONDS
    token_refresh_seconds: float = DEFAULT_HARDWARE_TOKEN_REFRESH_SECONDS
    watch_retry_seconds: float = DEFAULT_HARDWARE_WATCH_RETRY_SECONDS
    _devices: dict[str, DeviceDescriptor] = field(init=False, default_factory=dict)
    _advertisement: AdvertisementHandle | None = field(init=False, default=None)
    _advertiser: BeaconAdvertisementLease | None = field(init=False, default=None)
    _advertisement_id: str = field(init=False, default="")
    _advertised_payload: JsonObject | None = field(init=False, default=None)
    _advertisement_dirty: bool = field(init=False, default=True)
    _claims: dict[str, LiveHardwareClaim] = field(init=False, default_factory=dict)
    _claims_by_device: dict[str, LiveHardwareClaim] = field(
        init=False,
        default_factory=dict,
    )
    _claim_selection_device_ids: set[str] = field(init=False, default_factory=set)
    _claim_manager: ConcordParticipant = field(init=False)
    _lock: anyio.Lock = field(init=False, default_factory=anyio.Lock)
    _advertisement_lock: anyio.Lock = field(init=False, default_factory=anyio.Lock)
    _task_group: anyio.abc.TaskGroup | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.endpoint.address.family != "hardware_manager":
            raise ValueError(
                "hardware manager runtime endpoint must be hardware_manager"
            )
        if self.endpoint.address.endpoint_id != self.manager_id:
            raise ValueError("manager_id must match hardware_manager endpoint id")
        if self.advertisement_refresh_seconds <= 0:
            raise ValueError("advertisement_refresh_seconds must be greater than zero")
        if self.claim_reconcile_seconds <= 0:
            raise ValueError("claim_reconcile_seconds must be greater than zero")
        if self.token_refresh_seconds <= 0:
            raise ValueError("token_refresh_seconds must be greater than zero")
        if self.watch_retry_seconds <= 0:
            raise ValueError("watch_retry_seconds must be greater than zero")
        self._advertisement_id = f"hardware-{self.manager_id}-{uuid.uuid4()}"
        self._claim_manager = self.concord.participant(
            participant=self.endpoint.address,
            session_id=self.endpoint.session_id,
            profile=HARDWARE_CLAIM_PROFILE_ID,
            refresh_interval=self.token_refresh_seconds,
            reconcile_interval=self.claim_reconcile_seconds,
            log_label="Hardware",
            accept_contract=self._accept_claim_contract,
            current_sessions=self._claim_current_sessions,
            prepare_reconcile=self._prepare_claim_reconcile,
            contract_sort_key=self._claim_contract_sort_key,
        )

    @property
    def live_claims(self) -> tuple[LiveHardwareClaim, ...]:
        return tuple(self._claims[key] for key in sorted(self._claims))

    async def set_device(self, descriptor: DeviceDescriptor) -> None:
        self._devices[descriptor.device_id] = descriptor
        await self._publish_advertisement()
        await self._reconcile_claims(reason="device inventory changed")

    async def remove_device(
        self,
        device_id: str,
        reason: str = "removed",
    ) -> None:
        self._devices.pop(device_id, None)
        self._claims_by_device.pop(device_id, None)
        await self._cancel_claims_for_device(
            device_id,
            reason=f"hardware device {device_id} {reason}",
        )
        await self._publish_advertisement()
        await self._reconcile_claims(reason="device inventory changed")

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        await self._publish_advertisement()
        if self._advertiser is not None:
            self._advertiser.start(task_group)
        self._claim_manager.start(task_group)
        task_group.start_soon(self._command_subscription_loop)
        task_group.start_soon(self._contract_event_loop)
        task_group.start_soon(self._contract_reconcile_loop)

    async def stop(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._withdraw_advertisement()
            self._claims.clear()
            self._claims_by_device.clear()
            await self._claim_manager.aclose()
            self._task_group = None

    async def replace_devices(
        self,
        devices: Mapping[str, DeviceDescriptor],
        *,
        removed_reason: str = "removed",
    ) -> None:
        next_devices = dict(devices)
        previous = dict(self._devices)
        removed_device_ids = sorted(set(previous) - set(next_devices))
        self._devices = next_devices
        for device_id in removed_device_ids:
            self._claims_by_device.pop(device_id, None)
        for device_id in removed_device_ids:
            await self._cancel_claims_for_device(
                device_id,
                reason=f"hardware device {device_id} {removed_reason}",
            )
        await self._publish_advertisement()
        await self._reconcile_claims(reason="device snapshot changed")

    async def handle_hardware_message(self, message: DeckrMessage) -> bool:
        event = hw_messages.hardware_body_from_message(message)
        ref = hw_messages.hardware_device_ref_from_message(message)
        if ref is None or ref.manager_id != self.manager_id:
            return False
        if not isinstance(
            event,
            hw_messages.ControlInputMessage | hw_messages.CapabilityStateChangedMessage,
        ):
            return False
        if ref.device_id not in self._devices:
            logger.debug(
                "Dropping input/state for unknown hardware device %s/%s",
                ref.manager_id,
                ref.device_id,
            )
            return False
        claim = self._claims_by_device.get(ref.device_id)
        if claim is None:
            logger.debug(
                "Dropping unclaimed hardware input/state for %s/%s",
                ref.manager_id,
                ref.device_id,
            )
            return False
        await self.endpoint.send(
            lane=HARDWARE_MESSAGES_LANE,
            recipient=claim.controller_endpoint,
            recipient_session_id=claim.controller_session_id,
            message_type=message.message_type,
            body=hw_messages.hardware_body_to_dict(event),
            subject=message.subject,
            causation_id=message.causation_id,
        )
        return True

    async def _command_subscription_loop(self) -> None:
        async with self.endpoint.subscribe(HARDWARE_MESSAGES_LANE) as stream:
            async for envelope in stream:
                await self._handle_command(envelope)

    async def _handle_command(self, envelope: DeckrMessage) -> bool:
        ref = hw_messages.hardware_device_ref_from_message(envelope)
        if ref is None or ref.manager_id != self.manager_id:
            return False
        body = hw_messages.hardware_body_from_message(envelope)
        if not isinstance(
            body,
            hw_messages.ControlCommandMessage
            | hw_messages.CapabilityStateRequestMessage,
        ):
            return False
        if ref.device_id not in self._devices:
            await self._reject_command(envelope, body, reason="stale")
            return False
        claim = await self._command_claim(ref.device_id, envelope)
        if claim is None:
            await self._reject_command(envelope, body, reason="unauthorized")
            return False
        if envelope.sender_session_id != claim.controller_session_id:
            claim = await self._command_claim(
                ref.device_id,
                envelope,
                refresh=True,
            )
            if claim is None:
                await self._reject_command(envelope, body, reason="unauthorized")
                return False
            if envelope.sender_session_id != claim.controller_session_id:
                await self._reject_command(envelope, body, reason="stale")
                return False
        if self.command_handler is None:
            await self._reject_command(envelope, body, reason="unsupported")
            return False
        try:
            handled = await self.command_handler(envelope)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            handled = False
        if handled is None:
            await self._reject_command(envelope, body, reason="unsupported")
            return False
        if not handled:
            await self._reject_command(envelope, body, reason="stale")
            return False
        return True

    async def _command_claim(
        self,
        device_id: str,
        envelope: DeckrMessage,
        *,
        refresh: bool = False,
    ) -> LiveHardwareClaim | None:
        claim = self._claims_by_device.get(device_id)
        if refresh or claim is None or envelope.sender != claim.controller_endpoint:
            await self._reconcile_claims(reason="command authorization")
            claim = self._claims_by_device.get(device_id)
        if claim is None or envelope.sender != claim.controller_endpoint:
            return None
        return claim

    async def _publish_advertisement(self) -> None:
        async with self._advertisement_lock:
            payload = self._hardware_payload()
            payload_dict = payload.to_dict()
            try:
                if self._advertiser is None or self._advertiser.closed:
                    self._advertiser = await self.beacon.advertise(
                        BeaconAdvertisementSpec(
                            feature_id=HARDWARE_FEATURE_ID,
                            endpoint=self.endpoint.address,
                            session_id=self.endpoint.session_id,
                            advertisement_id=self._advertisement_id,
                            labels=payload.labels,
                            payload=payload_dict,
                            refresh_interval=self.advertisement_refresh_seconds,
                            log_label="Hardware",
                        ),
                    )
                    if self._task_group is not None:
                        self._advertiser.start(self._task_group)
                    self._advertisement = self._advertiser.handle
                else:
                    self._advertisement = await self._advertiser.update(
                        labels=payload.labels,
                        payload=payload_dict,
                    )
                self._advertised_payload = payload_dict
                self._advertisement_dirty = False
            except KvConflict:
                logger.info(
                    "Hardware Beacon advertisement changed; creating a fresh one",
                    exc_info=True,
                )
                self._advertisement = None
                self._advertiser = None
                self._advertised_payload = None
                self._advertisement_id = f"hardware-{self.manager_id}-{uuid.uuid4()}"
                self._advertisement_dirty = True
            except KvUnavailable:
                logger.warning(
                    "Hardware Beacon advertisements unavailable; retrying later"
                )
                self._advertisement_dirty = True

    async def _publish_advertisement_if_changed(self) -> None:
        async with self._advertisement_lock:
            payload = self._hardware_payload()
            payload_dict = payload.to_dict()
            if (
                not self._advertisement_dirty
                and self._advertiser is not None
                and not self._advertiser.closed
                and self._advertised_payload == payload_dict
            ):
                return

        await self._publish_advertisement()

    async def _withdraw_advertisement(self) -> None:
        async with self._advertisement_lock:
            self._advertisement = None
            self._advertised_payload = None
            self._advertisement_dirty = True
            if self._advertiser is not None:
                try:
                    await self._advertiser.aclose()
                except (KvConflict, KvUnavailable):
                    logger.debug("Could not withdraw hardware Beacon advertisement")
                self._advertiser = None

    async def _advertisement_refresh_loop(self) -> None:
        while True:
            await anyio.sleep(self.advertisement_refresh_seconds)
            await self._publish_advertisement()

    async def _contract_event_loop(self) -> None:
        async with self._claim_manager.watch() as stream:
            async for event in stream:
                await self._reconcile_claims(
                    reason=f"managed contract {event.event_type.value}"
                )

    async def _contract_reconcile_loop(self) -> None:
        while True:
            try:
                await self._reconcile_claims(reason="contract snapshot")
            except ConcordUnavailable:
                logger.warning(
                    "Hardware claim contracts unavailable; reconciliation will retry",
                    exc_info=True,
                )
            await anyio.sleep(self.claim_reconcile_seconds)

    async def _reconcile_claims(self, *, reason: str) -> None:
        async with self._lock:
            await self._reconcile_claims_locked(reason=reason)

    async def _reconcile_claims_locked(self, *, reason: str) -> None:
        logger.debug("Reconciling hardware manager claims via %s", reason)
        candidates = await self._matching_claim_candidates()
        ordered = self._ordered_claim_candidates(candidates)
        next_claims: dict[str, LiveHardwareClaim] = {}
        next_by_device: dict[str, LiveHardwareClaim] = {}

        for candidate in ordered:
            overlapping = set(candidate.device_ids) & set(next_by_device)
            if overlapping:
                continue
            if not candidate.valid or candidate.token is None:
                continue
            if candidate.controller_session_id is None:
                continue
            live = LiveHardwareClaim(
                contract=candidate.contract,
                terms=candidate.terms,
                manager_token=candidate.token,
                controller_endpoint=candidate.terms.controller_endpoint,
                controller_session_id=candidate.controller_session_id,
                device_refs=tuple(
                    device.device_ref for device in candidate.terms.devices
                ),
            )
            next_claims[candidate.contract.key] = live
            for device_id in live.device_ids:
                next_by_device[device_id] = live

        lost_claims = {
            key: claim for key, claim in self._claims.items() if key not in next_claims
        }
        self._claims = next_claims
        self._claims_by_device = next_by_device
        if lost_claims:
            await self._reset_lost_claim_devices(lost_claims.values())
        await self._publish_advertisement_if_changed()

    async def _matching_claim_candidates(self) -> dict[str, _ClaimCandidate]:
        candidates: dict[str, _ClaimCandidate] = {}
        for managed in await self._claim_manager.reconcile(reason="hardware runtime"):
            validity = managed.validity
            record = managed.record
            if record is None or record.state != ContractState.OPEN:
                continue
            try:
                terms = HardwareClaimTerms.model_validate(thaw_json(record.terms or {}))
            except ValueError:
                continue
            if not self._claim_terms_match_current_devices(terms):
                continue
            if self.endpoint.address not in managed.contract.participants:
                continue
            if terms.controller_endpoint not in managed.contract.participants:
                continue
            candidates[managed.contract.key] = _ClaimCandidate(
                contract=managed.contract,
                terms=terms,
                token=managed.token,
                valid=validity.status == ContractValidityStatus.VALID,
                controller_session_id=(
                    validity.tokens[str(terms.controller_endpoint)].session_id
                    if validity.status == ContractValidityStatus.VALID
                    and str(terms.controller_endpoint) in validity.tokens
                    else None
                ),
            )
        return candidates

    def _claim_terms_match_current_devices(
        self,
        terms: HardwareClaimTerms,
    ) -> bool:
        if terms.manager_endpoint != self.endpoint.address:
            return False
        for claim_device in terms.devices:
            ref = claim_device.device_ref
            descriptor = self._devices.get(ref.device_id)
            if descriptor is None:
                return False
            if ref.manager_id != self.manager_id:
                return False
            if ref.fingerprint not in {None, descriptor.fingerprint}:
                return False
        return True

    async def _accept_claim_contract(
        self,
        contract: ContractHandle,
        record: Any,
    ) -> bool:
        try:
            terms = HardwareClaimTerms.model_validate(thaw_json(record.terms or {}))
        except ValueError:
            return False
        if not (
            self._claim_terms_match_current_devices(terms)
            and self.endpoint.address in contract.participants
            and terms.controller_endpoint in contract.participants
        ):
            return False
        device_ids = {device.device_ref.device_id for device in terms.devices}
        if device_ids & self._claim_selection_device_ids:
            return False
        self._claim_selection_device_ids.update(device_ids)
        return True

    def _claim_current_sessions(self, _contract: ContractHandle) -> Mapping[str, str]:
        return {str(self.endpoint.address): self.endpoint.session_id}

    def _prepare_claim_reconcile(self) -> None:
        self._claim_selection_device_ids = set()

    def _claim_contract_sort_key(self, contract: ContractHandle) -> tuple[int, str]:
        return (0 if contract.key in self._claims else 1, contract.key)

    async def _cancel_claims_for_device(self, device_id: str, *, reason: str) -> None:
        claims = [
            claim for claim in self._claims.values() if device_id in claim.device_ids
        ]
        for claim in claims:
            try:
                cancelled = await self._claim_manager.cancel(
                    claim.contract,
                    reason=reason,
                )
            except (ConcordConflict, ConcordUnavailable):
                logger.debug(
                    "Could not cancel hardware claim %s for unavailable device %s",
                    claim.contract.contract_id,
                    device_id,
                    exc_info=True,
                )
                continue
            if cancelled:
                await self._claim_manager.release(claim.contract.key)

    def _ordered_claim_candidates(
        self,
        candidates: Mapping[str, _ClaimCandidate],
    ) -> tuple[_ClaimCandidate, ...]:
        existing = [
            candidates[key]
            for key in sorted(self._claims)
            if key in candidates and self._claims[key].contract.key == key
        ]
        existing_keys = {candidate.contract.key for candidate in existing}
        new = [
            candidate
            for key, candidate in sorted(candidates.items())
            if key not in existing_keys
        ]
        return tuple(existing + new)

    async def _reset_lost_claim_devices(
        self,
        claims: Any,
    ) -> None:
        if self.reset_handler is None:
            return
        reset_devices: set[str] = set()
        for claim in claims:
            reset_devices.update(claim.device_ids)
        for device_id in sorted(reset_devices):
            try:
                await self.reset_handler(device_id)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                logger.debug(
                    "Could not reset closed hardware device session %s", device_id
                )

    def _hardware_payload(self) -> HardwareBeaconPayload:
        claimed = set(self._claims_by_device)
        return HardwareBeaconPayload(
            managerId=self.manager_id,
            managerEndpoint=self.endpoint.address,
            sessionId=self.endpoint.session_id,
            labels=dict(self.labels or {}),
            devices={
                device_id: HardwareAdvertisementDevice(
                    capacity=ProfileCapacity(
                        totalInstances=1,
                        claimedInstances=1 if device_id in claimed else 0,
                        availableInstances=0 if device_id in claimed else 1,
                    ),
                    deviceRef=DeviceRef(
                        managerId=self.manager_id,
                        deviceId=device_id,
                        fingerprint=descriptor.fingerprint,
                    ),
                    descriptor=descriptor,
                )
                for device_id, descriptor in sorted(self._devices.items())
            },
        )

    async def _reject_command(
        self,
        envelope: DeckrMessage,
        body: hw_messages.ControlCommandMessage
        | hw_messages.CapabilityStateRequestMessage,
        *,
        reason: hw_messages.CommandRejectionReason,
    ) -> None:
        if isinstance(body, hw_messages.ControlCommandMessage):
            reply_body = hw_messages.CommandRejectedMessage(
                deviceRef=body.device_ref,
                controlId=body.control_id,
                capabilityId=body.capability_id,
                commandType=body.command_type,
                reason=reason,
                message=f"Hardware command {reason}",
            )
            await self.endpoint.reply_to(
                envelope,
                message_type=hw_messages.COMMAND_REJECTED,
                body=hw_messages.hardware_body_to_dict(reply_body),
                subject=envelope.subject,
                causation_id=envelope.causation_id,
            )
            return
        reply_body = hw_messages.CapabilityStateReplyMessage(
            deviceRef=body.device_ref,
            controlId=body.control_id,
            capabilityId=body.capability_id,
            stateType=body.state_type,
            status="rejected" if reason != "unsupported" else "unsupported",
            error=f"Hardware state request {reason}",
        )
        await self.endpoint.reply_to(
            envelope,
            message_type=hw_messages.CAPABILITY_STATE_REPLY,
            body=hw_messages.hardware_body_to_dict(reply_body),
            subject=envelope.subject,
            causation_id=envelope.causation_id,
        )


__all__ = [
    "DEFAULT_HARDWARE_ADVERTISEMENT_REFRESH_SECONDS",
    "DEFAULT_HARDWARE_CLAIM_RECONCILE_SECONDS",
    "DEFAULT_HARDWARE_TOKEN_REFRESH_SECONDS",
    "DEFAULT_HARDWARE_WATCH_RETRY_SECONDS",
    "HardwareManagerRuntime",
    "LiveHardwareClaim",
]
