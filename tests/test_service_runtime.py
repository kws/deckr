from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anyio
import pytest
from memory_lane_substrate import MemoryStateStore, memory_deckr

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import BeaconAdvertisementSpec, BeaconDiscovery, BeaconService
from deckr.concord import (
    ConcordCoordinator,
    ConcordService,
    ContractValidityStatus,
)
from deckr.contracts.messages import SERVICES_LANE, entity_subject, service_address
from deckr.services import (
    SERVICE_COMMAND_REPLY,
    AuthorizationDecision,
    ServiceAdvertisementPayload,
    ServiceAdvertiser,
    ServiceBackendStatus,
    ServiceCommandBody,
    ServiceCommandChannel,
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    ServiceDescriptor,
    ServiceError,
    ServiceProtocol,
    ServiceUseAuthorizer,
    ServiceUseLeaseManager,
    ServiceUseTerms,
    ServiceViewFamily,
    ServiceViewReader,
    ServiceViewRef,
    ServiceViewWriter,
    UnsupportedServiceScope,
    newest_service_descriptor,
    parse_service_descriptor,
    service_body,
    service_command_message,
    service_use_terms,
    service_view_key,
    service_view_prefix,
)


def _protocol(
    service_id: str = "openhab-home",
    *,
    operations: tuple[str, ...] = ("ensureItems", "refreshItem", "sendCommand"),
) -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.openhab.service",
        feature_id="dev.deckr.openhab.feature",
        advertisement_profile="dev.deckr.openhab.service.advertisement.v1",
        use_profile="dev.deckr.openhab.service_use.v1",
        operations=operations,
        view_families={
            "items": ServiceViewFamily(
                storeName="deckr_openhab_service_view_v1",
                keyPrefix=service_view_prefix(service_id, "items"),
            )
        },
    )


class CountingItemsStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.items_calls = 0

    async def items(self, prefix: str = ""):
        self.items_calls += 1
        return await super().items(prefix)


async def _publish_service_advertisement(
    beacon: BeaconService,
    protocol: ServiceProtocol,
    *,
    service_id: str = "openhab-home",
    session_id: str = "service-session",
    advertisement_id: str = "ad-1",
    payload: Mapping[str, Any] | None = None,
    feature_id: str | None = None,
):
    advertisement = await beacon.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=feature_id or protocol.feature_id,
            endpoint=service_address(service_id),
            session_id=session_id,
            advertisement_id=advertisement_id,
            payload=(
                dict(payload)
                if payload is not None
                else protocol.advertisement_payload(
                    service_id=service_id,
                    session_id=session_id,
                    backend_status=ServiceBackendStatus.AVAILABLE,
                ).to_dict()
            ),
        )
    )
    await advertisement.publish()
    return advertisement


async def _descriptor(
    beacon: BeaconService,
    protocol: ServiceProtocol,
) -> ServiceDescriptor:
    candidates = await beacon.find(protocol.feature_id)
    descriptors = [
        descriptor
        for candidate in candidates
        if (descriptor := parse_service_descriptor(candidate, protocol)) is not None
    ]
    descriptor = newest_service_descriptor(descriptors)
    assert descriptor is not None
    return descriptor


async def _service_reply_loop(endpoint, authorizer: ServiceUseAuthorizer) -> None:
    async with endpoint.subscribe() as stream:
        async for message in stream:
            body = service_body(message)
            if not isinstance(body, ServiceCommandBody):
                continue
            decision = await authorizer.authorize_command(message, body)
            if decision is AuthorizationDecision.AUTHORIZED:
                reply = ServiceCommandReplyBody(
                    serviceNamespace=body.service_namespace,
                    operation=body.operation,
                    status=ServiceCommandStatus.OK,
                    result={"accepted": True},
                )
            else:
                reply = ServiceCommandReplyBody(
                    serviceNamespace=body.service_namespace,
                    operation=body.operation,
                    status=ServiceCommandStatus.REJECTED,
                    error=ServiceError(
                        code="not_authorized",
                        message="Command is not authorized",
                    ),
                )
            await endpoint.reply_to(
                message,
                message_type=SERVICE_COMMAND_REPLY,
                body=reply.to_dict(),
            )


def test_service_protocol_payload_terms_and_view_keys() -> None:
    protocol = _protocol()
    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={"backend": "ok"},
    )

    assert protocol.feature_id != protocol.namespace
    assert service_view_key("openhab-home", "items", "Kitchen Light") == (
        "views.openhab-home.items.b64_S2l0Y2hlbiBMaWdodA"
    )
    assert payload.to_dict()["backendStatus"] == "available"
    assert payload.to_dict()["serviceUseProfile"] == protocol.use_profile
    assert payload.to_dict()["views"]["items"] == {
        "storeName": "deckr_openhab_service_view_v1",
        "keyPrefix": "views.openhab-home.items.",
    }
    assert ServiceAdvertisementPayload.model_validate(payload.to_dict()) == payload

    terms = ServiceUseTerms(
        profile=protocol.use_profile,
        serviceUseId="service-use:test",
        serviceId="openhab-home",
        serviceEndpoint=service_address("openhab-home"),
        serviceNamespace=protocol.namespace,
        serviceSessionId="service-session",
        clientEndpoint=action_provider_address("provider-main"),
        allowedOperations=("ensureItems",),
        allowedViews={"items": ("views.openhab-home.items.",)},
    )
    assert terms.to_dict()["allowedOperations"] == ["ensureItems"]
    assert terms.to_dict()["allowedViews"] == {
        "items": ["views.openhab-home.items."]
    }


@pytest.mark.asyncio
async def test_parse_service_descriptor_validates_profile_identity() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    candidate = (await beacon.find(protocol.feature_id))[0]

    descriptor = parse_service_descriptor(candidate, protocol)
    assert descriptor is not None
    assert descriptor.namespace == protocol.namespace
    assert descriptor.endpoint == service_address("openhab-home")

    wrong_feature_protocol = _protocol()
    object.__setattr__(wrong_feature_protocol, "feature_id", protocol.namespace)
    assert parse_service_descriptor(candidate, wrong_feature_protocol) is None

    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
    ).to_dict()
    payload["profile"] = "wrong-profile"
    await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-2",
        payload=payload,
    )
    wrong_profile = [
        item
        for item in await beacon.find(protocol.feature_id)
        if item.advertisement.advertisement_id == "ad-2"
    ][0]
    assert parse_service_descriptor(wrong_profile, protocol) is None

    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
    ).to_dict()
    payload["serviceNamespace"] = "wrong-namespace"
    await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-3",
        payload=payload,
    )
    wrong_namespace = [
        item
        for item in await beacon.find(protocol.feature_id)
        if item.advertisement.advertisement_id == "ad-3"
    ][0]
    assert parse_service_descriptor(wrong_namespace, protocol) is None


@pytest.mark.asyncio
async def test_service_use_terms_grant_only_requested_scope() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    descriptor = await _descriptor(beacon, protocol)

    terms = service_use_terms(
        descriptor,
        action_provider_address("provider-main"),
        operations={"ensureItems"},
        views={"items"},
    )

    assert terms.allowed_operations == ("ensureItems",)
    assert terms.allowed_views == {"items": ("views.openhab-home.items.",)}
    assert "sendCommand" not in terms.allowed_operations

    with pytest.raises(UnsupportedServiceScope):
        service_use_terms(
            descriptor,
            action_provider_address("provider-main"),
            operations={"missingOperation"},
        )
    with pytest.raises(UnsupportedServiceScope):
        service_use_terms(
            descriptor,
            action_provider_address("provider-main"),
            views={"missingView"},
        )


@pytest.mark.asyncio
async def test_explicit_service_lease_command_and_view_survive_beacon_loss() -> None:
    beacon_state = CountingItemsStateStore(name="beacon")
    beacon = BeaconService(BeaconDiscovery(beacon_state))
    concord = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
    )
    view_store = MemoryStateStore(name="views")
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint, deckr.lane(SERVICES_LANE).register_endpoint(
        action_provider_address("provider-main")
    ) as client_endpoint:
        advertiser = ServiceAdvertiser(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=beacon,
        )
        authorizer = ServiceUseAuthorizer(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            concord=concord,
        )
        writer = ServiceViewWriter(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            state=view_store,
        )
        await advertiser.publish(ServiceBackendStatus.AVAILABLE)
        descriptor = await _descriptor(beacon, protocol)
        key = service_view_key("openhab-home", "items", "Kitchen Light")
        view_ref = ServiceViewRef("deckr_openhab_service_view_v1", key)

        async with anyio.create_task_group() as tg:
            authorizer.start(tg)
            tg.start_soon(_service_reply_loop, service_endpoint, authorizer)
            leases = ServiceUseLeaseManager(
                endpoint=client_endpoint,
                concord=concord,
                task_group=tg,
            )
            lease = await leases.ensure(
                descriptor,
                operations={"ensureItems"},
                views={"items"},
                timeout=1.0,
            )
            commands = ServiceCommandChannel(endpoint=client_endpoint)
            views = ServiceViewReader(state_for=lambda _name: view_store)

            beacon_state.items_calls = 0
            reply = await commands.command(
                lease,
                "ensureItems",
                {"items": ["Kitchen Light"]},
            )
            assert reply.status == ServiceCommandStatus.OK

            await writer.put(key, {"item": "Kitchen Light", "state": "ON"})
            current = await views.read(lease, view_ref)
            assert current is not None
            assert current["state"] == "ON"

            await advertiser.withdraw()
            cached = await leases.cached(
                service_id="openhab-home",
                namespace=protocol.namespace,
                operations={"ensureItems"},
                views={"items"},
            )
            assert cached is lease
            reply = await commands.command(
                cached,
                "ensureItems",
                {"items": ["Kitchen Light"]},
            )
            assert reply.status == ServiceCommandStatus.OK
            current = await views.read(cached, view_ref)
            assert current is not None
            assert current["state"] == "ON"
            assert beacon_state.items_calls == 0

            rejected = await commands.command(lease, "sendCommand", {})
            assert rejected.status == ServiceCommandStatus.REJECTED
            assert rejected.error is not None
            assert rejected.error.code == "operation_not_authorized"

            await leases.aclose()
            await authorizer.aclose()
            await writer.withdraw()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_participant", ["client", "service"])
async def test_service_use_stale_contract_is_cancelled_and_superseded(
    missing_participant: str,
) -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    token_store = MemoryStateStore(name="tokens")
    concord = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            token_store,
        )
    )
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint, deckr.lane(SERVICES_LANE).register_endpoint(
        action_provider_address("provider-main")
    ) as client_endpoint:
        advertiser = ServiceAdvertiser(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=beacon,
        )
        authorizer = ServiceUseAuthorizer(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            concord=concord,
        )
        await advertiser.publish(ServiceBackendStatus.AVAILABLE)
        descriptor = await _descriptor(beacon, protocol)

        async with anyio.create_task_group() as tg:
            authorizer.start(tg)
            leases = ServiceUseLeaseManager(
                endpoint=client_endpoint,
                concord=concord,
                task_group=tg,
            )
            lease = await leases.ensure(
                descriptor,
                operations={"ensureItems"},
                timeout=1.0,
            )
            await authorizer.reconcile_contracts()
            client_token = lease.agreement.local_token
            managed = authorizer._manager.managed_contract(lease.contract)  # noqa: SLF001
            assert client_token is not None
            assert managed is not None
            assert managed.token is not None

            stale_token = (
                client_token if missing_participant == "client" else managed.token
            )
            await token_store.delete(stale_token.key, revision=stale_token.revision)
            await authorizer.reconcile_contracts()

            cancelled = await concord._validate(lease.contract)  # noqa: SLF001
            assert cancelled.status == ContractValidityStatus.CANCELLED

            replacement = await leases.ensure(
                descriptor,
                operations={"ensureItems"},
                timeout=1.0,
            )
            assert replacement.contract.contract_id == lease.contract.contract_id
            assert replacement.contract.generation == lease.contract.generation + 1
            assert (await replacement.agreement.refresh()).status == (
                ContractValidityStatus.VALID
            )

            await leases.aclose()
            await authorizer.aclose()
            await advertiser.withdraw()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_authorizer_not_applicable_for_wrong_target() -> None:
    concord = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
    )
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint:
        authorizer = ServiceUseAuthorizer(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            concord=concord,
        )
        body = ServiceCommandBody(
            serviceNamespace="wrong-namespace",
            operation="ensureItems",
        )
        message = service_command_message(
            sender=action_provider_address("provider-main"),
            sender_session_id="provider-session",
            recipient=service_endpoint.endpoint,
            recipient_session_id=service_endpoint.session_id,
            subject=entity_subject(
                "service",
                serviceId="openhab-home",
                namespace="wrong-namespace",
                operation="ensureItems",
            ),
            body=body,
        )

        assert (
            await authorizer.authorize_command(message, body)
            is AuthorizationDecision.NOT_APPLICABLE
        )

        body = ServiceCommandBody(
            serviceNamespace=protocol.namespace,
            operation="ensureItems",
        )
        message = service_command_message(
            sender=action_provider_address("provider-main"),
            sender_session_id="provider-session",
            recipient=service_endpoint.endpoint,
            recipient_session_id=service_endpoint.session_id,
            subject=entity_subject(
                "service",
                serviceId="other-service",
                namespace=protocol.namespace,
                operation="ensureItems",
            ),
            body=body,
        )

        assert (
            await authorizer.authorize_command(message, body)
            is AuthorizationDecision.NOT_APPLICABLE
        )
