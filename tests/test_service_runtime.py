from __future__ import annotations

import anyio
import pytest
from memory_lane_substrate import MemoryStateStore, memory_deckr

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import BeaconDiscovery, BeaconService, CandidateStatus
from deckr.concord import ConcordCoordinator, ConcordService, ContractState
from deckr.contracts.messages import SERVICES_LANE, entity_subject, service_address
from deckr.services import (
    GenericService,
    ServiceAdvertisementPayload,
    ServiceBackendStatus,
    ServiceClient,
    ServiceProtocol,
    ServiceUseTerms,
    ServiceViewFamily,
    ServiceViewRef,
    service_advertisement_from_candidate,
    service_command_message,
    service_use_terms,
    service_view_key,
    service_view_prefix,
)
from deckr.services.messages import ServiceCommandBody


def _protocol(service_id: str = "openhab-home") -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.openhab.service",
        feature_id="dev.deckr.openhab.service",
        advertisement_profile="dev.deckr.openhab.service.advertisement.v1",
        use_profile="dev.deckr.openhab.service_use.v1",
        operations=("ensureItems", "refreshItem", "sendCommand"),
        view_families={
            "items": ServiceViewFamily(
                storeName="deckr_openhab_service_view_v1",
                keyPrefix=service_view_prefix(service_id, "items"),
            )
        },
    )


def test_service_protocol_payload_terms_and_view_keys() -> None:
    protocol = _protocol()
    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={"backend": "ok"},
    )

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
        serviceAdvertisementId="ad-1",
        serviceSessionId="service-session",
        clientEndpoint=action_provider_address("provider-main"),
        allowedOperations=("ensureItems",),
    )

    assert terms.to_dict()["clientEndpoint"] == "action_provider:provider-main"


@pytest.mark.asyncio
async def test_service_client_views_and_beacon_loss_cancel_owned_contract() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    concord = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
    )
    view_store = MemoryStateStore(name="views")
    protocol = _protocol()
    service_endpoint = service_address("openhab-home")
    handle = await beacon.advertise(
        protocol.feature_id,
        service_endpoint,
        "service-session",
        payload=protocol.advertisement_payload(
            service_id="openhab-home",
            session_id="service-session",
            backend_status=ServiceBackendStatus.AVAILABLE,
        ).to_dict(),
    )

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        action_provider_address("provider-main")
    ) as client_endpoint:
        client = ServiceClient(
            endpoint=client_endpoint,
            beacon=beacon,
            concord=concord,
            state_for=lambda _name: view_store,
        )
        view = ServiceViewRef(
            "deckr_openhab_service_view_v1",
            service_view_key("openhab-home", "items", "Kitchen Light"),
        )

        assert (
            await client.read_view("openhab-home", protocol.namespace, view)
            is None
        )
        contract = (await concord.find_contracts(protocol.use_profile))[0]
        await concord.attach(contract, service_endpoint, "service-session")

        await view_store.put(
            view.key,
            {
                "serviceId": "openhab-home",
                "serviceNamespace": protocol.namespace,
                "sessionId": "old-session",
                "item": "Kitchen Light",
                "state": "ON",
            },
        )
        assert (
            await client.read_view("openhab-home", protocol.namespace, view)
            is None
        )

        await view_store.put(
            view.key,
            {
                "serviceId": "openhab-home",
                "serviceNamespace": protocol.namespace,
                "sessionId": "service-session",
                "item": "Kitchen Light",
                "state": "ON",
            },
        )
        current = await client.read_view("openhab-home", protocol.namespace, view)
        assert current is not None
        assert current["state"] == "ON"

        await client.aclose()
        cancelled = await concord.validate(contract)
        assert cancelled.contract is not None
        assert cancelled.contract.state == ContractState.CANCELLED

        replacement_client = ServiceClient(
            endpoint=client_endpoint,
            beacon=beacon,
            concord=concord,
            state_for=lambda _name: view_store,
        )
        assert (
            await replacement_client.read_view(
                "openhab-home",
                protocol.namespace,
                view,
            )
            is None
        )
        replacement_contracts = [
            item
            for item in await concord.find_contracts(protocol.use_profile)
            if item.contract_id == contract.contract_id
        ]
        assert [item.generation for item in replacement_contracts] == [1, 2]
        contract = replacement_contracts[1]
        assert contract.state == ContractState.OPEN
        await concord.attach(contract, service_endpoint, "service-session")
        current = await replacement_client.read_view(
            "openhab-home",
            protocol.namespace,
            view,
        )
        assert current is not None
        assert current["state"] == "ON"

        await beacon.withdraw(handle)
        assert (
            await replacement_client.read_view(
                "openhab-home",
                protocol.namespace,
                view,
            )
            is None
        )

    validity = await concord.validate(contract)
    assert validity.contract is not None
    assert validity.contract.state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_generic_service_advertise_authorize_and_withdraw() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
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
        service = GenericService(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=beacon,
            concord=concord,
            view_state=view_store,
            log_label="test-service",
        )
        await service.publish_status(ServiceBackendStatus.AVAILABLE)
        candidate = (await beacon.find(protocol.feature_id))[0]
        assert await beacon.validate(candidate) == CandidateStatus.CANDIDATE
        advertised = service_advertisement_from_candidate(
            candidate,
            protocol.namespace,
        )
        assert advertised is not None
        terms = service_use_terms(advertised, client_endpoint.endpoint)
        contract = await concord.create_contract(
            (service_endpoint.endpoint, client_endpoint.endpoint),
            contract_id=terms.service_use_id,
            profile=protocol.use_profile,
            terms=terms.to_dict(),
            created_by=client_endpoint.endpoint,
        )
        await concord.attach(
            contract,
            client_endpoint.endpoint,
            client_endpoint.session_id,
        )

        body = ServiceCommandBody(
            serviceNamespace=protocol.namespace,
            operation="ensureItems",
            params={"items": ["Kitchen Light"]},
        )
        message = service_command_message(
            sender=client_endpoint.endpoint,
            sender_session_id=client_endpoint.session_id,
            recipient=service_endpoint.endpoint,
            recipient_session_id=service_endpoint.session_id,
            subject=entity_subject("service"),
            body=body,
        )

        assert await service.command_authorized(message, body)

        rejected = ServiceCommandBody(
            serviceNamespace=protocol.namespace,
            operation="unknown",
        )
        assert not await service.command_authorized(message, rejected)

        key = service_view_key("openhab-home", "items", "Kitchen Light")
        await service.put_view(key, {"item": "Kitchen Light"})
        entry = await view_store.get(key)
        assert entry is not None
        assert entry.value["serviceId"] == "openhab-home"
        assert entry.value["serviceNamespace"] == protocol.namespace
        assert entry.value["sessionId"] == service_endpoint.session_id
        await service.withdraw()
        assert await beacon.validate(candidate) == CandidateStatus.MISSING
        assert await view_store.get(key) is None


@pytest.mark.asyncio
async def test_generic_service_refreshes_beacon_advertisement() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint:
        service = GenericService(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=beacon,
            concord=None,
            view_state=None,
            log_label="test-service",
            advertisement_refresh_interval=0.05,
        )
        await service.publish_status(ServiceBackendStatus.AVAILABLE)
        first = (await beacon.find(protocol.feature_id))[0]
        assert first.advertisement.refresh_seq == 1

        async with anyio.create_task_group() as tg:
            service.start(tg)
            with anyio.fail_after(1):
                while True:
                    current = (await beacon.find(protocol.feature_id))[0]
                    if current.advertisement.refresh_seq > first.advertisement.refresh_seq:
                        break
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
