from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest
from message_bus_mocks import mock_deckr

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME
from deckr.concord import (
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
    ConcordAgreementSpec,
    ConcordLifecyclePhase,
    ContractRecord,
    ContractState,
    concord_participant_token_key,
)
from deckr.contracts.messages import (
    entity_subject,
    service_address,
)
from deckr.services import (
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceExchangePattern,
    ServiceMessageBody,
    ServiceMessageDefinition,
    ServiceMessageDirection,
    ServiceMessageIntent,
    ServiceOperationDefinition,
    ServiceProtocol,
    ServiceUseAuthorizationError,
)
from deckr.services.messages import service_message
from deckr.substrates.nats_kv import KvUnavailable

pytestmark = pytest.mark.asyncio

SERVICE = service_address("demo-home")
CONSUMER = action_provider_address("demo-client")


def _protocol() -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.demo.service",
        feature_id="dev.deckr.demo.service.v1",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        operations={"play": ServiceOperationDefinition()},
        messages={
            "play": ServiceMessageDefinition(
                operation="play",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            )
        },
        view_families={},
    )


def _descriptor(protocol: ServiceProtocol, session_id: str) -> ServiceDescriptor:
    return ServiceDescriptor(
        candidate=None,
        service_id="demo-home",
        namespace=protocol.namespace,
        endpoint=SERVICE,
        session_id=session_id,
        advertisement_profile=protocol.advertisement_profile,
        use_profile=protocol.use_profile,
        supported_operations=frozenset(protocol.operations),
        supported_messages=protocol.messages,
        views={},
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={},
    )


async def _wait_for_valid(agreement) -> None:
    await agreement.wait_for_state(
        lambda state: state.valid
        or state.phase
        in {
            ConcordLifecyclePhase.UNAVAILABLE,
            ConcordLifecyclePhase.TERMINAL,
            ConcordLifecyclePhase.CLOSED,
        },
        timeout_seconds=2,
    )
    assert agreement.state.valid


def _request(agreement, *, sender_session_id: str = "consumer-session"):
    return service_message(
        sender=CONSUMER,
        sender_session_id=sender_session_id,
        recipient=SERVICE,
        recipient_session_id="service-session",
        subject=entity_subject(
            "service",
            serviceId="demo-home",
            namespace="dev.deckr.demo.service",
            name="play",
        ),
        body=ServiceMessageBody(
            serviceNamespace="dev.deckr.demo.service",
            name="play",
            intent=ServiceMessageIntent.COMMAND,
            exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
            params={"track": "one"},
        ),
        contract=agreement.contract.pointer,
    )


async def test_provider_bootstraps_before_advertising_and_authorizes_requests() -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
        deckr.endpoint(CONSUMER, session_id="consumer-session") as consumer_endpoint,
    ):
        provider = deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        )
        async with provider:
            assert provider.descriptor == _descriptor(
                protocol,
                service_endpoint.session_id,
            )
            assert deckr._message_bus.subscriptions[-1].entered  # noqa: SLF001
            candidates = deckr.beacon.candidates(protocol.feature_id)
            assert len(candidates) == 1
            assert candidates[0].advertisement.session_id == "service-session"

            agreement = deckr.concord.propose(
                consumer_endpoint,
                ConcordAgreementSpec(
                    profile=protocol.use_profile,
                    participants=(CONSUMER, SERVICE),
                    participant_sessions={str(SERVICE): "service-session"},
                ),
            )
            async with agreement:
                await _wait_for_valid(agreement)
                authorized = await provider.authorize(_request(agreement))
                assert authorized.contract.pointer == agreement.contract.pointer
                assert authorized.body.params == {"track": "one"}

                with pytest.raises(
                    ServiceUseAuthorizationError,
                    match="sender session",
                ):
                    await provider.authorize(
                        _request(agreement, sender_session_id="wrong-session")
                    )

        assert provider.state.phase == ConcordLifecyclePhase.CLOSED
        assert not deckr.beacon.candidates(protocol.feature_id)
        assert deckr._message_bus.subscriptions[-1].exited  # noqa: SLF001


async def test_provider_session_fence_cancels_without_creating_a_token() -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
        deckr.endpoint(CONSUMER, session_id="consumer-session") as consumer_endpoint,
        deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        ),
    ):
        agreement = deckr.concord.propose(
            consumer_endpoint,
            ConcordAgreementSpec(
                profile=protocol.use_profile,
                participants=(CONSUMER, SERVICE),
                participant_sessions={str(SERVICE): "replaced-session"},
            ),
        )
        async with agreement:
            with anyio.fail_after(2):
                while True:
                    record = await deckr.concord.contract_record(agreement.contract)
                    if record is not None and record.state == ContractState.CANCELLED:
                        break
                    await anyio.sleep(0)
            assert record.cancelled_by == SERVICE
            token_key = concord_participant_token_key(
                contract_id=agreement.contract.contract_id,
                generation=agreement.contract.generation,
                participant=SERVICE,
            )
            token_bucket = deckr._message_bus.kv_buckets[  # noqa: SLF001
                DEFAULT_CONCORD_TOKEN_BUCKET_NAME
            ]
            assert await token_bucket.get(token_key) is None


async def test_provider_stale_owner_cleanup_gates_subscription_and_beacon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
    ):
        stale = await deckr.concord._create_contract(  # noqa: SLF001
            (SERVICE, CONSUMER),
            contract_id="stale-provider-owned",
            profile=protocol.use_profile,
            created_by=SERVICE,
        )
        bucket = deckr._message_bus.kv_buckets[  # noqa: SLF001
            DEFAULT_CONCORD_CONTRACT_BUCKET_NAME
        ]
        original_update = bucket.update
        cancellation_started = anyio.Event()
        release_cancellation = anyio.Event()

        async def blocked_update(*args, **kwargs):
            cancellation_started.set()
            await release_cancellation.wait()
            return await original_update(*args, **kwargs)

        monkeypatch.setattr(bucket, "update", blocked_update)
        provider = deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        )

        async with anyio.create_task_group() as task_group:
            entered = anyio.Event()

            async def enter_provider() -> None:
                await provider.__aenter__()
                entered.set()

            task_group.start_soon(enter_provider)
            await cancellation_started.wait()
            assert not entered.is_set()
            assert deckr._message_bus.subscriptions == []  # noqa: SLF001
            assert not deckr.beacon.candidates(protocol.feature_id)
            release_cancellation.set()
            await entered.wait()
            await provider.aclose()

        entry = await bucket.get(stale.key)
        assert entry is not None
        assert ContractRecord.model_validate(entry.value).state == ContractState.CANCELLED


async def test_provider_failed_advertisement_closes_participant_and_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
    ):
        bucket = deckr._message_bus.kv_buckets[  # noqa: SLF001
            DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME
        ]
        monkeypatch.setattr(
            bucket,
            "create",
            AsyncMock(side_effect=KvUnavailable("beacon unavailable")),
        )
        provider = deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        )

        with pytest.raises(KvUnavailable, match="unresolved"):
            await provider.__aenter__()

        assert provider.state.phase == ConcordLifecyclePhase.CLOSED
        assert deckr._message_bus.subscriptions[-1].exited  # noqa: SLF001
        assert not deckr.beacon.candidates(protocol.feature_id)


async def test_provider_close_is_concurrent_safe() -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
    ):
        provider = deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        )
        await provider.__aenter__()
        async with anyio.create_task_group() as task_group:
            for _ in range(4):
                task_group.start_soon(provider.aclose)

        assert provider.state.phase == ConcordLifecyclePhase.CLOSED
        assert not deckr.beacon.candidates(protocol.feature_id)


async def test_provider_requests_publish_typed_authorization_outcomes() -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
        deckr.endpoint(CONSUMER, session_id="consumer-session") as consumer_endpoint,
        deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        ) as provider,
    ):
        agreement = deckr.concord.propose(
            consumer_endpoint,
            ConcordAgreementSpec(
                profile=protocol.use_profile,
                participants=(CONSUMER, SERVICE),
                participant_sessions={str(SERVICE): "service-session"},
            ),
        )
        async with agreement:
            await _wait_for_valid(agreement)
            stream = provider.requests()
            subscription = deckr._message_bus.subscriptions[-1]  # noqa: SLF001

            await subscription._send.send(_request(agreement))  # noqa: SLF001
            authorized = await anext(stream)
            assert authorized.authorization is not None
            assert authorized.failure is None
            assert authorized.authorization.contract.pointer == agreement.contract.pointer

            await subscription._send.send(  # noqa: SLF001
                _request(agreement, sender_session_id="wrong-session")
            )
            rejected = await anext(stream)
            assert rejected.authorization is None
            assert rejected.failure is not None
            assert rejected.failure.code == "contract_session_mismatch"
            await stream.aclose()


async def test_provider_update_and_capability_scoped_cancellation() -> None:
    protocol = _protocol()
    deckr = mock_deckr()

    async with (
        deckr,
        deckr.endpoint(SERVICE, session_id="service-session") as service_endpoint,
        deckr.endpoint(CONSUMER, session_id="consumer-session") as consumer_endpoint,
        deckr.service_provider(
            service_endpoint,
            protocol,
            service_id="demo-home",
        ) as provider,
    ):
        await provider.update(
            backend_status=ServiceBackendStatus.DEGRADED,
            diagnostics={"upstream": "slow"},
            labels={"site": "kitchen"},
        )
        assert provider.descriptor.backend_status == ServiceBackendStatus.DEGRADED
        assert provider.descriptor.diagnostics == {"upstream": "slow"}
        candidate = deckr.beacon.candidates(protocol.feature_id)[0]
        assert candidate.advertisement.labels == {"site": "kitchen"}
        assert candidate.advertisement.payload["backendStatus"] == "degraded"

        agreement = deckr.concord.propose(
            consumer_endpoint,
            ConcordAgreementSpec(
                profile=protocol.use_profile,
                participants=(CONSUMER, SERVICE),
                participant_sessions={str(SERVICE): "service-session"},
            ),
        )
        async with agreement:
            await _wait_for_valid(agreement)
            authorization = await provider.authorize(_request(agreement))
            assert await provider.cancel_authorized(
                authorization,
                reason="provider_test_cancel",
            )
            state = await agreement.wait_for_state(
                lambda current: current.phase == ConcordLifecyclePhase.TERMINAL,
                timeout_seconds=2,
            )
            assert state.phase == ConcordLifecyclePhase.TERMINAL
