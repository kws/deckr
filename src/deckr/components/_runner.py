import logging
from collections.abc import AsyncIterator, Awaitable

import anyio

from deckr.components._defs import (
    Component,
    ComponentLifecycleEvent,
    ComponentLifecycleEventType,
    ComponentState,
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
    on_started: Awaitable[None] | None = None,
) -> None:
    """Run a component with its own task group.

    The component controls its own concurrency through the task group.
    This function keeps running until cancelled or the component crashes.

    Args:
        component: The component to run
        stopping: Event to signal component should stop
        on_started: Optional awaitable to execute after component.start() succeeds
    """
    started = False
    try:
        # One TaskGroup per component: the component "controls its own concurrency"
        async with anyio.create_task_group() as tg:
            ctx = RunContext(tg=tg, stopping=stopping)
            await component.start(ctx)
            started = True

            # Component started successfully - execute callback if provided
            if on_started:
                await on_started

            # Keep the runner alive until manager cancels it.
            # When cancelled, the TaskGroup unwinds and cancels all component tasks.
            await anyio.sleep_forever()
    except BaseException:
        # Call stop() on cancellation or any other exception
        # Only call if component was successfully started and stop() hasn't been called yet
        # (If stopping is already set, _stop_component is handling the shutdown)
        if started and not stopping.is_set():
            try:
                # Signal that component should stop
                stopping.set()
                # Call component's stop() method for cleanup
                await component.stop()
            except Exception as e:
                logger.warning(
                    f"Error calling stop() on component '{component.name}': {e}",
                    exc_info=True,
                )
        raise


class ComponentManager(Component):
    """Registry and lifecycle manager for components.

    Manages component lifecycle (start, stop, crash detection) with proper
    state tracking, error handling, and resource cleanup. Similar to OSGi
    lifecycle management but without service discovery.
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
        self._tg: anyio.TaskGroup | None = None

    async def run(self) -> None:
        """Start the service manager event loop.

        This should be run in a task group. The manager will process
        component lifecycle events until cancelled.
        """
        if self._tg is not None:
            raise RuntimeError("ComponentManager already started")

        async with anyio.create_task_group() as tg:
            self._tg = tg
            await self._event_loop()

    async def start(self, ctx: RunContext) -> None:
        """Start the ComponentManager as a sub-component of the given task group.

        This will start the event loop in the given task group.
        """
        if self._tg is not None:
            raise RuntimeError("ComponentManager already started")
        ctx.tg.start_soon(self.run)

    async def stop(self) -> None:
        if self._tg is not None:
            self._tg.cancel_scope.cancel()
        await self._event_send.aclose()

    async def add_component(self, component: Component) -> None:
        """Add a component to the service registry.

        Args:
            plugin: The component to add

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
        """Remove a component from the service registry.

        This is idempotent - removing a non-existent component is a no-op.

        Args:
            plugin: The component to remove
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
                if event.plugin.name != name:
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
        started = anyio.Event()
        error_occurred = False

        async def _runner_wrapper() -> None:
            """Wrapper that handles crash detection and cleanup."""
            nonlocal error_occurred
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
                            cancel_scope=cs,
                            state=ComponentState.STARTING,
                        )
                        self._running[component.name] = rc

                    started.set()

                    # Run the component (this calls component.start() and then sleeps)
                    # We'll update state to RUNNING after start() succeeds
                    async def set_running():
                        async with self._lock:
                            if component.name in self._running:
                                self._running[
                                    component.name
                                ].state = ComponentState.RUNNING
                        try:
                            await self._subscribers.push(
                                ComponentLifecycleEvent(
                                    component, ComponentLifecycleEventType.STARTED
                                )
                            )
                        except SubscribableQueue.SubscriberBufferFullError:
                            pass

                    # Create the coroutine and pass it - component_runner will await it after start() succeeds
                    # If component.start() fails, we'll await it in the finally block to prevent warnings
                    set_running_coro = set_running()
                    coro_awaited = False
                    try:
                        await component_runner(
                            component, stopping, on_started=set_running_coro
                        )
                        coro_awaited = True  # component_runner awaited it successfully
                    finally:
                        # Ensure coroutine is awaited even if component_runner fails early
                        # This prevents "coroutine was never awaited" warnings
                        if not coro_awaited:
                            try:
                                await set_running_coro
                            except (RuntimeError, StopAsyncIteration):
                                # Coroutine was already awaited or completed - ignore
                                pass

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
                "ServiceManager.run() must be called before adding components"
            )

        self._tg.start_soon(_runner_wrapper, name=f"component:{component.name}")
        await started.wait()

    async def _stop_component(self, name: str, *, stop_timeout_s: float = 5.0) -> None:
        """Stop a component gracefully with timeout fallback.

        Args:
            name: Component name to stop
            stop_timeout_s: Timeout for graceful stop in seconds
        """
        # Remove from registry under lock, but perform awaits outside the lock
        async with self._lock:
            rc = self._running.pop(name, None)

        if rc is None:
            # Component not found - this is idempotent
            return

        # Update state to STOPPING
        rc.state = ComponentState.STOPPING

        # Graceful phase: signal stop and wait for component.stop()
        rc.stopping.set()

        try:
            with anyio.move_on_after(stop_timeout_s) as scope:
                await rc.component.stop()

            # Hard phase if stop timed out
            if scope.cancel_called:
                logger.warning(
                    f"Component '{name}' stop() timed out after {stop_timeout_s}s, "
                    "forcing cancellation"
                )
            else:
                # Successfully stopped gracefully
                pass

            rc.state = ComponentState.STOPPED

        except Exception as e:
            logger.error(f"Error stopping component '{name}': {e}", exc_info=True)
            rc.state = ComponentState.FAILED
        finally:
            # Always cancel the scope to kill the component's task group, even if
            # we timed out or were cancelled by an outer shutdown timeout
            rc.cancel_scope.cancel()

    async def _event_loop(self) -> None:
        """Main event loop processing component lifecycle events.

        Handles ADDED, REMOVED, and CRASHED events with proper error handling.
        """
        try:
            async for event in self._event_receive:
                try:
                    if event.event_type == ComponentLifecycleEventType.ADDED:
                        await self._start_component(event.plugin)
                    elif event.event_type == ComponentLifecycleEventType.REMOVED or event.event_type == ComponentLifecycleEventType.CRASHED:
                        await self._stop_component(event.plugin.name)

                    try:
                        await self._subscribers.push(event)
                    except SubscribableQueue.SubscriberBufferFullError:
                        pass

                except Exception as e:
                    # Log but don't crash the event loop
                    logger.error(
                        f"Error processing {event.event_type} event for "
                        f"'{event.plugin.name}': {e}",
                        exc_info=True,
                    )
        except anyio.get_cancelled_exc_class():
            # Normal cancellation - stop all components before exiting
            # Close send side to unblock receive loop if it's still waiting
            try:
                await self._event_send.aclose()
            except Exception:
                pass  # Ignore errors closing stream
            await self._stop_all_components()
            raise
        except Exception as e:
            # Unexpected error in event loop
            logger.critical(
                f"Fatal error in service manager event loop: {e}", exc_info=True
            )
            try:
                await self._event_send.aclose()
            except Exception:
                pass  # Ignore errors closing stream
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
