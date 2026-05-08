from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from deckr.contracts.messages import SERVICES_LANE, service_address
from deckr.services.state import (
    ServiceCatalog,
    ServiceStatus,
    ServiceStatusValue,
    service_catalog_key,
    service_status_key,
)
from deckr.state import EndpointPresence, presence_endpoint_key
from pydantic import ValidationError

from deckr_python_runtime.state import StateStore, StateUnavailable


class ServiceLiveState(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ServiceLiveCheck:
    state: ServiceLiveState
    service_id: str
    service_namespace: str
    session_id: str | None = None
    reason: str | None = None
    catalog: ServiceCatalog | None = None
    status: ServiceStatus | None = None


async def live_service_check(
    lease_state: StateStore,
    discovery_state: StateStore,
    *,
    service_id: str,
    service_namespace: str,
) -> ServiceLiveCheck:
    endpoint = service_address(service_id)
    presence_entry = await lease_state.get(
        presence_endpoint_key(lane=SERVICES_LANE, endpoint=endpoint)
    )
    if presence_entry is None:
        return ServiceLiveCheck(
            state=ServiceLiveState.ABSENT,
            service_id=service_id,
            service_namespace=service_namespace,
            reason="presence_absent",
        )
    try:
        presence = EndpointPresence.model_validate(presence_entry.value)
    except ValidationError:
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            reason="presence_invalid",
        )
    if (
        presence.lane != SERVICES_LANE
        or presence.endpoint != endpoint
        or not presence.session_id
    ):
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            reason="presence_mismatch",
        )

    catalog_entry = await discovery_state.get(service_catalog_key(service_id))
    if catalog_entry is None:
        return ServiceLiveCheck(
            state=ServiceLiveState.ABSENT,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="catalog_absent",
        )
    status_entry = await discovery_state.get(service_status_key(service_id))
    if status_entry is None:
        return ServiceLiveCheck(
            state=ServiceLiveState.ABSENT,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="status_absent",
        )

    try:
        catalog = ServiceCatalog.model_validate(catalog_entry.value)
        status = ServiceStatus.model_validate(status_entry.value)
    except ValidationError:
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="discovery_invalid",
        )

    if catalog.service_namespace != service_namespace:
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="catalog_namespace_mismatch",
        )
    if status.service_namespace != service_namespace:
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="status_namespace_mismatch",
        )
    if (
        catalog.session_id != presence.session_id
        or status.session_id != presence.session_id
    ):
        return ServiceLiveCheck(
            state=ServiceLiveState.INVALID,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=presence.session_id,
            reason="session_mismatch",
            catalog=catalog,
            status=status,
        )
    if status.status == ServiceStatusValue.AVAILABLE:
        state = ServiceLiveState.AVAILABLE
    elif status.status == ServiceStatusValue.DEGRADED:
        state = ServiceLiveState.DEGRADED
    else:
        state = ServiceLiveState.UNAVAILABLE
    return ServiceLiveCheck(
        state=state,
        service_id=service_id,
        service_namespace=service_namespace,
        session_id=presence.session_id,
        catalog=catalog,
        status=status,
    )


async def service_is_live(
    lease_state: StateStore,
    discovery_state: StateStore,
    *,
    service_id: str,
    service_namespace: str,
) -> bool:
    try:
        check = await live_service_check(
            lease_state,
            discovery_state,
            service_id=service_id,
            service_namespace=service_namespace,
        )
    except StateUnavailable:
        return False
    return check.state == ServiceLiveState.AVAILABLE


__all__ = [
    "ServiceLiveCheck",
    "ServiceLiveState",
    "live_service_check",
    "service_is_live",
]
