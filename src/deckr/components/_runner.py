from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

import anyio

from deckr.components._defs import (
    Component,
    ComponentLifecycleEvent,
    ComponentLifecycleEventType,
    ComponentState,
    ComponentStatus,
    ReadinessState,
    RunContext,
    RunningComponent,
)
from deckr.core.util.anyio import SubscribableQueue

# State ordering for "min_state" / "or higher" semantics.
# FAILED = -1 so it never satisfies "wait for RUNNING" (can't distinguish
# crash-during-start from crash-after-start).
_STATE_ORDER: dict[ComponentState, int] = {
    ComponentState.IDLE: 0,
    ComponentState.STARTING: 1,
    ComponentState.RUNNING: 2,
    ComponentState.STOPPING: 3,
    ComponentState.STOPPED: 4,
    ComponentState.FAILED: -1,
}

logger = logging.getLogger(__name__)

# Timeout per component during shutdown; components that don't stop within this
# are force-killed via cancel_scope.cancel()
SHUTDOWN_TIMEOUT_PER_COMPONENT = 2.0


async def component_runner(
    component: Component,
    stopping: anyio.Event,
    on_started: Callable[[], Awaitable[None]] | None = None,
    status_reporter: _ManagerStatusReporter | None = None,
    stop_timeout_s: Callable[[], float | None] | None = None,
    record_stop_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run a component with its own task group.

    The component controls its own concurrency through the task group.
    This function keeps running until stopped, cancelled, or the component crashes.

    Args:
        component: The component to run
        stopping: Event to signal component should stop
        on_started: Optional callback to execute after component.start() succeeds
    """
    started = False
    # One TaskGroup per component: the component "controls its own concurrency".
    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping, status=status_reporter)
        await component.start(ctx)
        started = True

        try:
            # Component started successfully - execute callback if provided
            if on_started:
                await on_started()

            # Keep the runner alive until the manager asks it to stop. If the
            # surrounding scope is cancelled instead, this task still owns the
            # component cleanup before the component TaskGroup unwinds.
            await stopping.wait()
        finally:
            if started:
                stopping.set()
                timeout = (
                    stop_timeout_s() if stop_timeout_s is not None else None
                ) or SHUTDOWN_TIMEOUT_PER_COMPONENT
                with anyio.move_on_after(timeout, shield=True) as scope:
                    try:
                        await component.stop()
                    except Exception as e:
                        if record_stop_error is not None:
                            record_stop_error(e)
                        logger.warning(
                            f"Error calling stop() on component '{component.name}': {e}",
                            exc_info=True,
                        )
                if scope.cancel_called:
                    logger.warning(
                        f"Component '{component.name}' stop() timed out after "
                        f"{timeout}s"
                    )
            tg.cancel_scope.cancel()


class _ManagerStatusReporter:
    def __init__(self, manager: ComponentManager, component_name: str) -> None:
        self._manager = manager
        self._component_name = component_name

    async def report(
        self,
        readiness_state: ReadinessState,
        *,
        reasons: Sequence[str] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        await self._manager._report_component_readiness(
            self._component_name,
            readiness_state,
            reasons=reasons,
            diagnostics=diagnostics,
        )


class ComponentManager(Component):
    """Registry and lifecycle manager for components.

    Manages component lifecycle (start, stop, crash detection) with proper
    state tracking, error handling, and resource cleanup. Similar to OSGi
    lifecycle management but without a generic object registry.
    """

    name = "ComponentManager"

    def __init__(self):
        self._lock = anyio.Lock()
        self._running: dict[str, RunningComponent] = {}
        # Use unbounded buffer to avoid blocking on add/remove
        # Note: max_buffer_size must be an int, not float('inf')
        # Use a very large number instead
        self._event_send, self._event_receive = anyio.create_memory_object_stream(
            max_buffer_size=10000
        )
        self._subscribers = SubscribableQueue[ComponentLifecycleEvent]()
        self._status_subscribers = SubscribableQueue[ComponentStatus]()
        self._tg: anyio.TaskGroup | None = None
        self._run_finished = anyio.Event()

    async def run(
        self,
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Start the component manager event loop.

        This should be run in a task group. The manager will process
        component lifecycle events until cancelled.
        """
        if self._tg is not None:
            raise RuntimeError("ComponentManager already started")

        self._run_finished = anyio.Event()
        try:
            async with anyio.create_task_group() as tg:
                self._tg = tg
                task_status.started()
                await self._event_loop()
        finally:
            self._tg = None
            self._run_finished.set()

    async def start(self, ctx: RunContext) -> None:
        """Start the ComponentManager as a sub-component of the given task group.

        This will start the event loop in the given task group.
        """
        if self._tg is not None:
            raise RuntimeError("ComponentManager already started")
        await ctx.tg.start(self.run)

    async def stop(self) -> None:
        with anyio.CancelScope(shield=True):
            tg = self._tg
            if tg is not None:
                await self._stop_all_components()
                tg.cancel_scope.cancel()
            await self._event_send.aclose()
            if tg is not None:
                await self._run_finished.wait()

    async def add_component(self, component: Component) -> None:
        """Add a component to the runtime registry.

        Args:
            component: The component to add

        Raises:
            ValueError: If component is missing name attribute
            RuntimeError: If component with same name already exists
        """
        # Validate component has name
        if not hasattr(component, "name") or not component.name:
            raise ValueError(
                f"Component must have a non-empty 'name' attribute: {component}"
            )

        # Check for duplicate before sending event
        async with self._lock:
            if component.name in self._running:
                state = self._running[component.name].state
                raise RuntimeError(
                    f"Component '{component.name}' already exists in state {state}"
                )

        # Send event (non-blocking due to unbounded buffer)
        await self._event_send.send(
            ComponentLifecycleEvent(component, ComponentLifecycleEventType.ADDED)
        )

    async def remove_component(self, component: Component) -> None:
        """Remove a component from the runtime registry.

        This is idempotent - removing a non-existent component is a no-op.

        Args:
            component: The component to remove
        """
        await self._event_send.send(
            ComponentLifecycleEvent(component, ComponentLifecycleEventType.REMOVED)
        )

    def get_component_state(self, name: str) -> ComponentState | None:
        """Get the current state of a component.

        Args:
            name: Component name

        Returns:
            ComponentState if component exists, None otherwise
        """
        # Note: This is synchronous and doesn't need async lock
        # We're just reading the dict, which is safe in Python
        # For thread-safety, we'd need the lock, but anyio is single-threaded
        rc = self._running.get(name)
        return rc.state if rc else None

    def get_component_status(self, name: str) -> ComponentStatus | None:
        rc = self._running.get(name)
        return _status_from_running(rc) if rc is not None else None

    def list_component_statuses(self) -> list[ComponentStatus]:
        return [
            _status_from_running(rc)
            for _name, rc in sorted(self._running.items(), key=lambda item: item[0])
        ]

    def list_components(self) -> list[str]:
        """List all registered component names.

        Returns:
            List of component names
        """
        return list(self._running.keys())

    def _state_satisfies(
        self, current: ComponentState | None, target: ComponentState, min_state: bool
    ) -> bool:
        """Check if current state satisfies target (exact or min_state)."""
        if current is None:
            return False
        if target == ComponentState.FAILED:
            return current == ComponentState.FAILED
        if min_state:
            order = _STATE_ORDER.get(current, -1)
            target_order = _STATE_ORDER.get(target, -1)
            return order >= target_order and order >= 0
        return current == target

    async def wait_for_state(
        self,
        component_or_name: Component | str,
        target_state: ComponentState,
        *,
        timeout: float = 5.0,
        min_state: bool = True,
    ) -> None:
        """Wait until component reaches target_state (or higher) or timeout.

        Event-driven: checks current state first, then subscribes to lifecycle
        events. No polling.

        Args:
            component_or_name: Component instance or name string
            target_state: State to wait for (e.g. ComponentState.RUNNING)
            timeout: Max seconds to wait
            min_state: If True, accept target_state or any "higher" state
                (RUNNING, STOPPING, STOPPED). FAILED never satisfies
                RUNNING (can't distinguish crash-during-start).

        Raises:
            TimeoutError: If state not reached within timeout
        """
        name = (
            component_or_name.name
            if hasattr(component_or_name, "name")
            else component_or_name
        )

        def _check() -> bool:
            state = self.get_component_state(name)
            return self._state_satisfies(state, target_state, min_state)

        if _check():
            return

        with anyio.move_on_after(timeout) as scope:
            async for event in self.subscribe():
                if event.component.name != name:
                    continue
                if event.event_type == ComponentLifecycleEventType.CRASHED:
                    # Fail fast: component crashed before reaching target
                    if not _check():
                        raise RuntimeError(
                            f"Component '{name}' crashed before reaching "
                            f"state {target_state}"
                        )
                if _check():
                    return

        if scope.cancel_called:
            current = self.get_component_state(name)
            raise TimeoutError(
                f"Component '{name}' did not reach state {target_state} "
                f"within {timeout}s. Current state: {current}"
            )

    async def _start_component(self, component: Component) -> None:
        """Start a component with proper error handling and state tracking.

        Args:
            component: The component to start
        """
        stopping = anyio.Event()
        stopped = anyio.Event()
        started = anyio.Event()
        error_occurred = False
        running_component: RunningComponent | None = None

        async def _runner_wrapper() -> None:
            """Wrapper that handles crash detection and cleanup."""
            nonlocal error_occurred, running_component
            cs: anyio.CancelScope | None = None

            try:
                # Capture cancel scope for this component
                with anyio.CancelScope() as cs:
                    # Register handle promptly (under lock) so stop/remove can find it
                    async with self._lock:
                        # Double-check for duplicate (race condition protection)
                        if component.name in self._running:
                            logger.warning(
                                f"Component '{component.name}' already exists, skipping start"
                            )
                            return

                        rc = RunningComponent(
                            component=component,
                            stopping=stopping,
                            stopped=stopped,
                            cancel_scope=cs,
                            state=ComponentState.STARTING,
                        )
                        running_component = rc
                        self._running[component.name] = rc
                        status = _status_from_running(rc)

                    started.set()
                    await self._push_status(status)

                    # Run the component (this calls component.start() and then sleeps)
                    # We'll update state to RUNNING after start() succeeds
                    async def set_running():
                        async with self._lock:
                            if component.name in self._running:
                                rc = self._running[component.name]
                                rc.state = ComponentState.RUNNING
                                status = _status_from_running(rc)
                            else:
                                status = None
                        if status is not None:
                            await self._push_status(status)
                        try:
                            await self._subscribers.push(
                                ComponentLifecycleEvent(
                                    component, ComponentLifecycleEventType.STARTED
                                )
                            )
                        except SubscribableQueue.SubscriberBufferFullError:
                            pass

                    def current_stop_timeout() -> float | None:
                        if running_component is None:
                            return None
                        return running_component.stop_timeout_s

                    def record_stop_error(exc: Exception) -> None:
                        if running_component is not None:
                            running_component.stop_error = exc

                    await component_runner(
                        component,
                        stopping,
                        on_started=set_running,
                        status_reporter=_ManagerStatusReporter(self, component.name),
                        stop_timeout_s=current_stop_timeout,
                        record_stop_error=record_stop_error,
                    )

            except BaseException as e:
                # Check if this is an expected cancellation (normal shutdown)
                if isinstance(e, anyio.get_cancelled_exc_class()):
                    # Expected cancellation during shutdown - not an error
                    async with self._lock:
                        rc = self._running.get(component.name)
                        if rc:
                            rc.state = ComponentState.STOPPED
                    # Re-raise to let task group handle it
                    raise

                # Actual crash - log and handle
                error_occurred = True
                logger.error(
                    f"Component '{component.name}' crashed: {e}", exc_info=True
                )

                # Update state to FAILED
                async with self._lock:
                    rc = self._running.get(component.name)
                    if rc:
                        rc.state = ComponentState.FAILED
                        status = _status_from_running(rc)
                    else:
                        status = None
                if status is not None:
                    await self._push_status(status)

                # Emit CRASHED event for cleanup
                try:
                    await self._event_send.send(
                        ComponentLifecycleEvent(
                            component, ComponentLifecycleEventType.CRASHED
                        )
                    )
                except Exception as send_error:
                    logger.error(
                        f"Failed to send CRASHED event for '{component.name}': {send_error}"
                    )

                # Re-raise to let task group handle it
                raise
            finally:
                stopped.set()
                # Clean up if we never successfully started
                if not started.is_set() or error_occurred:
                    async with self._lock:
                        rc = self._running.pop(component.name, None)
                        if rc and cs:
                            # Cancel the scope if we have it
                            cs.cancel()

        # Start runner
        if self._tg is None:
            raise RuntimeError(
                "ComponentManager.run() must be called before adding components"
            )

        self._tg.start_soon(_runner_wrapper, name=f"component:{component.name}")
        await started.wait()

    async def _stop_component(self, name: str, *, stop_timeout_s: float = 5.0) -> None:
        """Stop a component gracefully with timeout fallback.

        Args:
            name: Component name to stop
            stop_timeout_s: Timeout for graceful stop in seconds
        """
        async with self._lock:
            rc = self._running.get(name)
            if rc is not None:
                rc.state = ComponentState.STOPPING
                rc.stop_timeout_s = stop_timeout_s
                status = _status_from_running(rc)
            else:
                status = None

        if rc is None:
            # Component not found - this is idempotent
            return

        if status is not None:
            await self._push_status(status)

        # Graceful phase: ask the component runner to call stop() from the same
        # task that called start(), then wait for that runner to finish.
        rc.stopping.set()
        with anyio.move_on_after(stop_timeout_s + 0.25, shield=True) as scope:
            await rc.stopped.wait()

        # Hard phase if the runner did not stop within its budget.
        if scope.cancel_called:
            logger.warning(
                f"Component '{name}' did not stop after {stop_timeout_s}s, "
                "forcing cancellation"
            )
            rc.cancel_scope.cancel()
            with anyio.move_on_after(0.25, shield=True):
                await rc.stopped.wait()

        async with self._lock:
            current = self._running.get(name)
            if current is rc:
                rc.state = (
                    ComponentState.FAILED
                    if rc.stop_error is not None
                    else ComponentState.STOPPED
                )
                status = _status_from_running(rc)
                self._running.pop(name, None)
            else:
                status = None

        if status is not None:
            await self._push_status(status)

    async def _event_loop(self) -> None:
        """Main event loop processing component lifecycle events.

        Handles ADDED, REMOVED, and CRASHED events with proper error handling.
        """
        try:
            async for event in self._event_receive:
                try:
                    if event.event_type == ComponentLifecycleEventType.ADDED:
                        await self._start_component(event.component)
                    elif event.event_type in {
                        ComponentLifecycleEventType.REMOVED,
                        ComponentLifecycleEventType.CRASHED,
                    }:
                        await self._stop_component(event.component.name)

                    try:
                        await self._subscribers.push(event)
                    except SubscribableQueue.SubscriberBufferFullError:
                        pass

                except Exception as e:
                    # Log but don't crash the event loop
                    logger.error(
                        f"Error processing {event.event_type} event for "
                        f"'{event.component.name}': {e}",
                        exc_info=True,
                    )
        except anyio.get_cancelled_exc_class():
            # Normal cancellation - stop all components before exiting
            # Close send side to unblock receive loop if it's still waiting
            try:
                await self._event_send.aclose()
            except Exception:
                pass  # Ignore errors closing stream
            with anyio.CancelScope(shield=True):
                await self._stop_all_components()
            raise
        except Exception as e:
            # Unexpected error in event loop
            logger.critical(
                f"Fatal error in component manager event loop: {e}", exc_info=True
            )
            try:
                await self._event_send.aclose()
            except Exception:
                pass  # Ignore errors closing stream
            with anyio.CancelScope(shield=True):
                await self._stop_all_components()
            raise

    async def _stop_all_components(self) -> None:
        """Stop all running components. Used during shutdown.

        Each component gets SHUTDOWN_TIMEOUT_PER_COMPONENT seconds to stop
        gracefully; components that don't respond are force-killed.
        """
        # Get all component names (snapshot to avoid modification during iteration)
        async with self._lock:
            names = list(self._running.keys())

        timeout = SHUTDOWN_TIMEOUT_PER_COMPONENT
        stop_timeout = timeout / 2  # Graceful stop gets half the budget

        for name in names:
            try:
                with anyio.move_on_after(timeout) as scope:
                    await self._stop_component(name, stop_timeout_s=stop_timeout)
                if scope.cancel_called:
                    logger.warning(
                        f"Timeout stopping component '{name}' during shutdown "
                        f"(>{timeout}s), force-killed"
                    )
            except Exception as e:
                logger.error(f"Error stopping component '{name}' during shutdown: {e}")

    async def subscribe(self) -> AsyncIterator[ComponentLifecycleEvent]:
        async for event in self._subscribers.subscribe():
            yield event

    async def subscribe_status(self) -> AsyncIterator[ComponentStatus]:
        async for status in self._status_subscribers.subscribe():
            yield status

    async def _report_component_readiness(
        self,
        name: str,
        readiness_state: ReadinessState,
        *,
        reasons: Sequence[str] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        normalized_reasons = tuple(_normalize_status_reason(reason) for reason in reasons)
        normalized_diagnostics = dict(diagnostics or {})
        async with self._lock:
            rc = self._running.get(name)
            if rc is None:
                return
            rc.local_readiness_state = readiness_state
            rc.local_readiness_reasons = normalized_reasons
            rc.local_diagnostics = normalized_diagnostics
            _refresh_effective_readiness(rc)
            status = _status_from_running(rc)
        await self._push_status(status)

    async def _push_status(self, status: ComponentStatus) -> None:
        try:
            await self._status_subscribers.push(status)
        except SubscribableQueue.SubscriberBufferFullError:
            pass


def _normalize_status_reason(reason: str) -> str:
    if not reason:
        raise ValueError("component readiness reason must not be empty")
    return reason


def _refresh_effective_readiness(rc: RunningComponent) -> None:
    rc.readiness_state = rc.local_readiness_state
    rc.readiness_reasons = rc.local_readiness_reasons
    rc.diagnostics = dict(rc.local_diagnostics)


def _status_from_running(rc: RunningComponent) -> ComponentStatus:
    return ComponentStatus(
        runtime_name=rc.component.name,
        lifecycle_state=rc.state,
        readiness_state=rc.readiness_state,
        readiness_reasons=rc.readiness_reasons,
        diagnostics=dict(rc.diagnostics),
    )
