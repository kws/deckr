from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anyio
import pytest
from memory_lane_substrate import MemoryStateStore, memory_deckr

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import (
    BeaconAdvertisementSpec,
    BeaconDiscovery,
    BeaconService,
    CandidateStatus,
)
from deckr.concord import (
    ConcordCoordinator,
    ConcordService,
    ContractState,
    ContractValidityStatus,
)
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
from deckr.state import StateUnavailable


def _protocol(
    service_id: str = "openhab-home",
    *,
    operations: tuple[str, ...] = ("ensureItems", "refreshItem", "sendCommand"),
) -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.openhab.service",
        feature_id="dev.deckr.openhab.service",
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


class FailingItemsStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.items_calls = 0

    async def items(self, prefix: str = ""):
        self.items_calls += 1
        if self.items_calls == 1:
            raise StateUnavailable("broker unavailable")
        return await super().items(prefix)


class CountingItemsStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.items_calls = 0

    async def items(self, prefix: str = ""):
        self.items_calls += 1
        return await super().items(prefix)


class FailingDeleteStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.delete_calls = 0

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        self.delete_calls += 1
        raise StateUnavailable("broker unavailable")


class FailingCreateOnceStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.create_calls = 0

    async def create(self, *args, **kwargs):
        self.create_calls += 1
        if self.create_calls == 1:
            raise StateUnavailable("broker unavailable")
        return await super().create(*args, **kwargs)


async def _publish_service_advertisement(
    beacon: BeaconService,
    protocol: ServiceProtocol,
    *,
    service_id: str = "openhab-home",
    session_id: str = "service-session",
    advertisement_id: str | None = None,
):
    endpoint = service_address(service_id)
    advertisement = await beacon.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=protocol.feature_id,
            endpoint=endpoint,
            session_id=session_id,
            advertisement_id=advertisement_id,
            payload=protocol.advertisement_payload(
                service_id=service_id,
                session_id=session_id,
                backend_status=ServiceBackendStatus.AVAILABLE,
            ).to_dict(),
        )
    )
    await advertisement.publish()
    return advertisement


async def _establish_view_lease(
    client: ServiceClient,
    concord: ConcordService,
    protocol: ServiceProtocol,
    service_endpoint,
    view: ServiceViewRef,
    *,
    service_id: str = "openhab-home",
    session_id: str = "service-session",
):
    result: dict[str, Mapping[str, Any] | None] = {}

    async def read_view() -> None:
        result["value"] = await client.read_view(service_id, protocol.namespace, view)

    async with anyio.create_task_group() as tg:
        tg.start_soon(read_view)
        with anyio.fail_after(1):
            while True:
                contracts = [
                    item
                    for item in await concord._find_contracts(protocol.use_profile)
                    if item.state == ContractState.OPEN
                ]
                if contracts:
                    contract = contracts[-1]
                    break
                await anyio.sleep(0.01)
        await concord._attach(contract, service_endpoint, session_id)
        with anyio.fail_after(1):
            while "value" not in result:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()
    return contract


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
        serviceSessionId="service-session",
        clientEndpoint=action_provider_address("provider-main"),
        allowedOperations=("ensureItems",),
    )

    terms_dict = terms.to_dict()
    assert terms_dict["clientEndpoint"] == "action_provider:provider-main"
    assert "serviceAdvertisementId" not in terms_dict


@pytest.mark.asyncio
async def test_service_client_views_survive_beacon_loss_with_valid_contract() -> None:
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
    advertisement = await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-1",
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

        contract = await _establish_view_lease(
            client,
            concord,
            protocol,
            service_endpoint,
            view,
        )

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
        cancelled = await concord._validate(contract)
        assert cancelled.contract is not None
        assert cancelled.contract.state == ContractState.CANCELLED

        replacement_client = ServiceClient(
            endpoint=client_endpoint,
            beacon=beacon,
            concord=concord,
            state_for=lambda _name: view_store,
        )
        contract = await _establish_view_lease(
            replacement_client,
            concord,
            protocol,
            service_endpoint,
            view,
        )
        replacement_contracts = [
            item
            for item in await concord._find_contracts(protocol.use_profile)
            if item.contract_id == contract.contract_id
        ]
        assert [item.generation for item in replacement_contracts] == [1, 2]
        assert contract.state == ContractState.OPEN
        current = await replacement_client.read_view(
            "openhab-home",
            protocol.namespace,
            view,
        )
        assert current is not None
        assert current["state"] == "ON"

        await advertisement.aclose()
        await _publish_service_advertisement(
            beacon,
            protocol,
            session_id="replacement-service-session",
            advertisement_id="ad-2",
        )
        current = await replacement_client.read_view(
            "openhab-home",
            protocol.namespace,
            view,
        )
        assert current is not None
        assert current["state"] == "ON"

    validity = await concord._validate(contract)
    assert validity.contract is not None
    assert validity.status == ContractValidityStatus.VALID
    assert validity.contract.state == ContractState.OPEN


@pytest.mark.asyncio
async def test_service_client_reuses_contract_across_advertisement_id_change() -> None:
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
    first_advertisement = await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-1",
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

        contract = await _establish_view_lease(
            client,
            concord,
            protocol,
            service_endpoint,
            view,
        )

        await first_advertisement.aclose()
        await _publish_service_advertisement(
            beacon,
            protocol,
            advertisement_id="ad-2",
        )
        await client.read_view("openhab-home", protocol.namespace, view)

    contracts = await concord._find_contracts(protocol.use_profile)
    assert [item.key for item in contracts] == [contract.key]
    validity = await concord._validate(contract)
    assert validity.status == ContractValidityStatus.VALID


@pytest.mark.asyncio
async def test_service_client_reuses_valid_cached_lease_without_beacon_scan() -> None:
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
    service_endpoint = service_address("openhab-home")
    await _publish_service_advertisement(beacon, protocol)

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

        await _establish_view_lease(
            client,
            concord,
            protocol,
            service_endpoint,
            view,
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

        beacon_state.items_calls = 0
        current = await client.read_view("openhab-home", protocol.namespace, view)

    assert current is not None
    assert current["state"] == "ON"
    assert beacon_state.items_calls == 0


@pytest.mark.asyncio
async def test_service_client_new_operation_does_not_cancel_existing_valid_lease() -> None:
    beacon = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    concord = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
    )
    view_store = MemoryStateStore(name="views")
    initial_protocol = _protocol(operations=("ensureItems",))
    expanded_protocol = _protocol(operations=("ensureItems", "sendCommand"))
    service_endpoint = service_address("openhab-home")
    first_advertisement = await _publish_service_advertisement(
        beacon,
        initial_protocol,
        advertisement_id="ad-1",
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
        old_contract = await _establish_view_lease(
            client,
            concord,
            initial_protocol,
            service_endpoint,
            view,
        )

        await first_advertisement.aclose()
        await _publish_service_advertisement(
            beacon,
            expanded_protocol,
            session_id="service-session-2",
            advertisement_id="ad-2",
        )
        reply = await client.command(
            "openhab-home",
            expanded_protocol.namespace,
            "sendCommand",
            timeout=0.01,
        )

    assert reply.error is not None
    assert reply.error.code == "service_contract_pending"
    assert (await concord._validate(old_contract)).status == (
        ContractValidityStatus.VALID
    )


@pytest.mark.asyncio
async def test_service_client_pending_lease_rediscovery_waits_for_call_timeout() -> None:
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
    first_advertisement = await _publish_service_advertisement(
        beacon,
        protocol,
        session_id="service-session-1",
        advertisement_id="ad-1",
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
        beacon_state.items_calls = 0
        result: dict[str, str | None] = {}

        async def call_service() -> None:
            reply = await client.command(
                "openhab-home",
                protocol.namespace,
                "sendCommand",
                timeout=0.03,
            )
            result["code"] = reply.error.code if reply.error is not None else None

        async with anyio.create_task_group() as tg:
            tg.start_soon(call_service)
            with anyio.fail_after(1):
                while not (await concord._find_contracts(protocol.use_profile)):
                    await anyio.sleep(0.01)
            await first_advertisement.aclose()
            await _publish_service_advertisement(
                beacon,
                protocol,
                session_id="service-session-2",
                advertisement_id="ad-2",
            )
            assert beacon_state.items_calls == 1
            with anyio.fail_after(1):
                while "code" not in result:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert result["code"] == "service_contract_pending"
        await client.command(
            "openhab-home",
            protocol.namespace,
            "sendCommand",
            timeout=0.01,
        )
        assert beacon_state.items_calls >= 2


@pytest.mark.asyncio
async def test_service_client_rediscover_before_rejecting_unknown_operation() -> None:
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
    service_endpoint = service_address("openhab-home")
    await _publish_service_advertisement(beacon, protocol)

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
        await _establish_view_lease(
            client,
            concord,
            protocol,
            service_endpoint,
            view,
        )

        beacon_state.items_calls = 0
        reply = await client.command(
            "openhab-home",
            protocol.namespace,
            "missingOperation",
            timeout=0.01,
        )

    assert reply.error is not None
    assert reply.error.code == "unsupported_operation"
    assert beacon_state.items_calls == 1


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
        contract = await concord._create_contract(
            (service_endpoint.endpoint, client_endpoint.endpoint),
            contract_id=terms.service_use_id,
            profile=protocol.use_profile,
            terms=terms.to_dict(),
            created_by=client_endpoint.endpoint,
        )
        await concord._attach(
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
        assert service._advertiser is not None
        await service._advertiser.aclose()
        service._advertisement = None
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
@pytest.mark.parametrize("missing_participant", ["client", "service"])
async def test_generic_service_cancels_stale_service_use_contract(
    caplog,
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
        service = GenericService(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=beacon,
            concord=concord,
            view_state=None,
            log_label="test-service",
        )
        await service.publish_status(ServiceBackendStatus.AVAILABLE)
        candidate = (await beacon.find(protocol.feature_id))[0]
        advertised = service_advertisement_from_candidate(
            candidate,
            protocol.namespace,
        )
        assert advertised is not None
        terms = service_use_terms(advertised, client_endpoint.endpoint)
        contract = await concord._create_contract(
            (service_endpoint.endpoint, client_endpoint.endpoint),
            contract_id=terms.service_use_id,
            profile=protocol.use_profile,
            terms=terms.to_dict(),
            created_by=client_endpoint.endpoint,
        )
        client_token = await concord._attach(
            contract,
            client_endpoint.endpoint,
            client_endpoint.session_id,
        )
        service_token = await concord._attach(
            contract,
            service_endpoint.endpoint,
            service_endpoint.session_id,
        )
        token = client_token if missing_participant == "client" else service_token
        await token_store.delete(token.key, revision=token.revision)

        caplog.set_level("INFO", logger="deckr.concord")
        caplog.clear()
        await service.reconcile_contracts()

    validity = await concord._validate(contract)
    assert validity.status == ContractValidityStatus.CANCELLED
    assert "Concord contract invalid" not in caplog.text


@pytest.mark.asyncio
async def test_generic_service_reconcile_loop_retries_state_unavailable() -> None:
    contract_state = FailingItemsStateStore(name="contracts")
    concord = ConcordService(
        ConcordCoordinator(
            contract_state,
            MemoryStateStore(name="tokens"),
        )
    )
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint:
        service = GenericService(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=None,
            concord=concord,
            view_state=None,
            reconcile_interval=0.01,
        )

        async with anyio.create_task_group() as tg:
            tg.start_soon(service.contract_reconcile_loop)
            with anyio.fail_after(1):
                while contract_state.items_calls < 2:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_generic_service_withdraw_ignores_unavailable_view_cleanup() -> None:
    view_store = FailingDeleteStateStore(name="views")
    protocol = _protocol()

    async with memory_deckr() as deckr, deckr.lane(SERVICES_LANE).register_endpoint(
        service_address("openhab-home")
    ) as service_endpoint:
        service = GenericService(
            protocol=protocol,
            service_id="openhab-home",
            endpoint=service_endpoint,
            beacon=None,
            concord=None,
            view_state=view_store,
        )
        await service.put_view(
            service_view_key("openhab-home", "items", "Kitchen Light"),
            {"item": "Kitchen Light"},
        )

        await service.withdraw()

    assert view_store.delete_calls == 1


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


@pytest.mark.asyncio
async def test_generic_service_withdraw_closes_advertiser_without_handle() -> None:
    beacon = BeaconService(BeaconDiscovery(FailingCreateOnceStateStore(name="beacon")))
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
            advertisement_refresh_interval=0.01,
        )

        async with anyio.create_task_group() as tg:
            service.start(tg)
            await service.publish_status(ServiceBackendStatus.AVAILABLE)
            assert service.advertisement is None
            await service.withdraw()
            await anyio.sleep(0.03)
            tg.cancel_scope.cancel()

    assert await beacon.find(protocol.feature_id) == ()
