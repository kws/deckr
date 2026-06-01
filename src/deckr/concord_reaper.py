from __future__ import annotations

import anyio

from deckr.components import (
    BaseComponent,
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
    RunContext,
)
from deckr.concord import (
    CONCORD_CONTRACT_STORE_POLICY,
    CONCORD_MAINTENANCE_STORE_POLICY,
    CONCORD_TOKEN_STORE_POLICY,
    DEFAULT_CONCORD_CONTRACT_STORE_NAME,
    DEFAULT_CONCORD_MAINTENANCE_STORE_NAME,
    DEFAULT_CONCORD_TOKEN_STORE_NAME,
    ConcordCoordinator,
    ConcordReaperConfig,
    ConcordReaperService,
    ConcordService,
)

CONCORD_REAPER_COMPONENT_ID = "dev.deckr.concord.reaper"


class ConcordReaperComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        service: ConcordReaperService,
    ) -> None:
        super().__init__(name=runtime_name)
        self._service = service

    @property
    def service(self) -> ConcordReaperService:
        return self._service

    async def start(self, ctx: RunContext) -> None:
        ctx.start_task(
            self._run,
            ctx.stopping,
            name=f"{self.name}.concord-reaper",
        )
        await ctx.report_ready(
            diagnostics={
                "scan_interval_seconds": self._service.config.scan_interval_seconds,
                "stale_grace_seconds": self._service.config.stale_grace_seconds,
                "cancelled_retention_seconds": (
                    self._service.config.cancelled_retention_seconds
                ),
            }
        )

    async def stop(self) -> None:
        await self._service.aclose()

    async def _run(self, stopping: anyio.Event) -> None:
        await self._service.run(stop_event=stopping)


def component_factory(context: ComponentContext) -> ConcordReaperComponent:
    config = ConcordReaperConfig.model_validate(dict(context.config))
    contract_state = context.state(
        DEFAULT_CONCORD_CONTRACT_STORE_NAME,
        policy=CONCORD_CONTRACT_STORE_POLICY,
    )
    token_state = context.state(
        DEFAULT_CONCORD_TOKEN_STORE_NAME,
        policy=CONCORD_TOKEN_STORE_POLICY,
    )
    maintenance_state = context.state(
        DEFAULT_CONCORD_MAINTENANCE_STORE_NAME,
        policy=CONCORD_MAINTENANCE_STORE_POLICY,
    )
    concord = ConcordService(ConcordCoordinator(contract_state, token_state))
    service = ConcordReaperService(
        concord,
        contract_state=contract_state,
        token_state=token_state,
        maintenance_state=maintenance_state,
        config=config,
    )
    return ConcordReaperComponent(
        runtime_name=context.runtime_name,
        service=service,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id=CONCORD_REAPER_COMPONENT_ID,
        role="concord_maintenance",
    ),
    factory=component_factory,
)

__all__ = [
    "CONCORD_REAPER_COMPONENT_ID",
    "ConcordReaperComponent",
    "component",
    "component_factory",
]
