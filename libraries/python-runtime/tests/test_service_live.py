from __future__ import annotations

from datetime import UTC, datetime

import pytest
from deckr.contracts.messages import SERVICES_LANE, service_address
from deckr.services.state import ServiceCatalog, ServiceStatus, ServiceStatusValue
from deckr.state import EndpointPresence, presence_endpoint_key
from memory_lane_substrate import MemoryStateStore

from deckr_python_runtime.services import ServiceLiveState, live_service_check


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_live_service_check_requires_presence_catalog_status_and_session() -> None:
    lease = MemoryStateStore(name="lease")
    discovery = MemoryStateStore(name="discovery")
    endpoint = service_address("sonos-home")
    await lease.put(
        presence_endpoint_key(lane=SERVICES_LANE, endpoint=endpoint),
        EndpointPresence(
            endpoint=endpoint,
            lane=SERVICES_LANE,
            sessionId="service-session",
            timestamp=_now(),
            ttlSeconds=30,
        ),
    )
    await discovery.put(
        "catalog.services.sonos-home",
        ServiceCatalog(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            supportedOperations=("play", "pause"),
            viewPrefixes=("view.services.sonos-home",),
            timestamp=_now(),
        ),
    )
    await discovery.put(
        "status.services.sonos-home",
        ServiceStatus(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            status=ServiceStatusValue.AVAILABLE,
            timestamp=_now(),
        ),
    )

    check = await live_service_check(
        lease,
        discovery,
        service_id="sonos-home",
        service_namespace="dev.deckr.sonos.service",
    )

    assert check.state == ServiceLiveState.AVAILABLE
    assert check.session_id == "service-session"

    await discovery.put(
        "status.services.sonos-home",
        ServiceStatus(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="old-session",
            status=ServiceStatusValue.AVAILABLE,
            timestamp=_now(),
        ),
    )

    stale = await live_service_check(
        lease,
        discovery,
        service_id="sonos-home",
        service_namespace="dev.deckr.sonos.service",
    )
    assert stale.state == ServiceLiveState.INVALID
    assert stale.reason == "session_mismatch"
