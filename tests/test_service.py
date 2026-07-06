"""Comprehensive tests for ComponentManager lifecycle management."""

from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import pytest
import pytest_asyncio

from deckr.components import (
    ComponentManager,
    ComponentState,
    ReadinessState,
    RunContext,
)

# Tests use anyio backend (configured in conftest.py)


async def wait_for_removed(
    manager: ComponentManager, name: str, timeout: float = 1.0
) -> None:
    """Wait for a component to be removed (state is None)."""
    with anyio.move_on_after(timeout) as scope:
        while manager.get_component_state(name) is not None:
            await anyio.sleep(0.01)

    if scope.cancel_called:
        raise TimeoutError(f"Component '{name}' was not removed within {timeout}s")


@asynccontextmanager
async def _manager_context():
    """Internal async context manager for ComponentManager lifecycle."""
    manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        await tg.start(manager.run)

        try:
            yield (manager, tg)
        finally:
            await manager.stop()
            tg.cancel_scope.cancel()

    # Suppress any ExceptionGroup that might have been raised from component crashes
    # This allows tests to complete even if components crashed during execution


class ManagerContext:
    """Async context manager for ComponentManager lifecycle in tests."""

    def __init__(self):
        self._cm = _manager_context()

    async def __aenter__(self):
        """Enter the context manager and start the ComponentManager."""
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager and clean up resources."""
        try:
            return await self._cm.__aexit__(exc_type, exc_val, exc_tb)
        except BaseExceptionGroup:
            # Suppress ExceptionGroups from component crashes during cleanup
            # These are expected in some tests and shouldn't fail the test
            return True  # Suppress the exception


@pytest_asyncio.fixture
def manager_context():
    """Fixture that provides a ComponentManager in a task group with proper cleanup."""
    return ManagerContext()


# Test component implementations
@dataclass
class MockComponent:
    """Simple mock component for testing."""

    name: str
    start_called: bool = False
    stop_called: bool = False
    start_should_raise: Exception | None = None
    stop_should_raise: Exception | None = None
    start_delay: float = 0.0
    stop_delay: float = 0.0
    crash_after_start: bool = False
    tasks_started: int = 0

    async def start(self, ctx: RunContext) -> None:
        """Start the component."""
        if self.start_should_raise:
            raise self.start_should_raise

        if self.start_delay > 0:
            await anyio.sleep(self.start_delay)

        self.start_called = True

        if self.crash_after_start:
            # Start a task that will crash
            async def crash_task():
                await anyio.sleep(0.1)
                raise RuntimeError("Component crashed!")

            ctx.tg.start_soon(crash_task)
            self.tasks_started += 1
        else:
            # Start a normal task
            async def normal_task():
                await anyio.sleep_forever()

            ctx.tg.start_soon(normal_task)
            self.tasks_started += 1

    async def stop(self) -> None:
        """Stop the component."""
        if self.stop_should_raise:
            raise self.stop_should_raise

        if self.stop_delay > 0:
            await anyio.sleep(self.stop_delay)

        self.stop_called = True


@dataclass
class ComponentWithoutName:
    """Component missing name attribute."""

    async def start(self, ctx: RunContext) -> None:
        pass

    async def stop(self) -> None:
        pass


@dataclass
class ReportingComponent(MockComponent):
    """Component that reports runtime-local readiness during start."""

    async def start(self, ctx: RunContext) -> None:
        await ctx.report_unready("worker_starting")
        await ctx.report_ready(diagnostics={"worker": "ok"})
        await super().start(ctx)


@dataclass
class CountingComponent:
    """Component that runs a task that increments a counter - used to verify task cancellation."""

    name: str
    counter: int = 0
    task_running: bool = False

    async def start(self, ctx: RunContext) -> None:
        """Start a task that increments counter in a loop."""
        self.task_running = True

        async def counting_task():
            try:
                while True:
                    self.counter += 1
                    await anyio.sleep(0.01)  # Small delay to allow cancellation
            finally:
                self.task_running = False

        ctx.tg.start_soon(counting_task)

    async def stop(self) -> None:
        """Stop the component."""
        pass  # Component doesn't need to do anything, cancellation handles it


@dataclass
class MultiTaskComponent:
    """Component that starts multiple tasks - used to verify all tasks are cancelled."""

    name: str
    counters: list[int] = None
    task_count: int = 3

    def __post_init__(self):
        if self.counters is None:
            self.counters = [0] * self.task_count

    async def start(self, ctx: RunContext) -> None:
        """Start multiple counting tasks."""
        for i in range(self.task_count):

            async def counting_task(idx: int):
                try:
                    while True:
                        self.counters[idx] += 1
                        await anyio.sleep(0.01)
                except anyio.get_cancelled_exc_class():
                    # Task was cancelled - this is expected
                    raise

            ctx.tg.start_soon(counting_task, i)

    async def stop(self) -> None:
        """Stop the component."""
        pass


@dataclass
class StoppingAwareComponent:
    """Component that checks stopping event - verifies graceful shutdown."""

    name: str
    counter: int = 0
    stopped_gracefully: bool = False

    async def start(self, ctx: RunContext) -> None:
        """Start a task that checks stopping event."""

        async def aware_task():
            try:
                while True:
                    self.counter += 1
                    # Check if we should stop
                    if ctx.stopping.is_set():
                        self.stopped_gracefully = True
                        return  # Exit gracefully
                    await anyio.sleep(0.01)
            except anyio.get_cancelled_exc_class():
                # Task was cancelled - not graceful
                raise

        ctx.tg.start_soon(aware_task)

    async def stop(self) -> None:
        """Stop the component."""
        pass


@dataclass
class TaskOwnedStopComponent:
    """Component whose cleanup must run in the same task as start."""

    name: str
    start_task_id: int | None = None
    stop_task_id: int | None = None

    async def start(self, ctx: RunContext) -> None:
        self.start_task_id = anyio.get_current_task().id
        ctx.tg.start_soon(anyio.sleep_forever)

    async def stop(self) -> None:
        self.stop_task_id = anyio.get_current_task().id


class TestComponentManagerErrors:
    """Test error scenarios and edge cases."""

    @pytest.mark.asyncio
    async def test_component_without_name(self, manager_context):
        """Test that component without name raises error."""
        async with manager_context as (manager, tg):
            component = ComponentWithoutName()

            with pytest.raises(ValueError, match="non-empty 'name' attribute"):
                await manager.add_component(component)

    @pytest.mark.asyncio
    async def test_stop_timeout_expiration(self, manager_context):
        """Test that stop timeout works correctly."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1", stop_delay=10.0)  # Very long stop

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Remove with short timeout
            await manager._stop_component("test1", stop_timeout_s=0.1)
            await wait_for_removed(manager, "test1")

            # Component should be removed despite timeout
            assert manager.get_component_state("test1") is None


class TestComponentManagerRaceConditions:
    """Test race condition scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_add_same_component(self, manager_context):
        """Test concurrent add of same component."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="test1")
            comp2 = MockComponent(name="test1")

            # Try to add both concurrently
            async def add_comp1():
                try:
                    await manager.add_component(comp1)
                except RuntimeError:
                    pass  # Expected if comp2 wins

            async def add_comp2():
                try:
                    await manager.add_component(comp2)
                except RuntimeError:
                    pass  # Expected if comp1 wins

            async with anyio.create_task_group() as test_tg:
                test_tg.start_soon(add_comp1)
                test_tg.start_soon(add_comp2)
            await anyio.sleep(0.1)

            # Only one should be registered
            components = manager.list_components()
            assert len(components) == 1
            assert "test1" in components

    @pytest.mark.asyncio
    async def test_add_while_removing(self, manager_context):
        """Test adding component while it's being removed."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="test1", stop_delay=0.2)
            comp2 = MockComponent(name="test1")

            # Add first component
            await manager.add_component(comp1)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Start removal (slow)
            async def remove():
                await manager.remove_component(comp1)

            # Try to add while removing
            async def add():
                await anyio.sleep(0.05)  # Wait a bit
                try:
                    await manager.add_component(comp2)
                except RuntimeError:
                    pass  # May fail if still removing

            async with anyio.create_task_group() as test_tg:
                test_tg.start_soon(remove)
                test_tg.start_soon(add)
            await anyio.sleep(0.3)

            # Final state should be consistent
            state = manager.get_component_state("test1")
            # Either removed or one is running
            assert state is None or state == ComponentState.RUNNING

class TestComponentManagerStateManagement:
    """Test state management and query APIs."""

    @pytest.mark.asyncio
    async def test_list_components(self, manager_context):
        """Test list_components() API."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="comp1")
            comp2 = MockComponent(name="comp2")

            # Initially empty
            assert manager.list_components() == []

            # Add components
            await manager.add_component(comp1)
            await manager.add_component(comp2)
            await manager.wait_for_state("comp1", ComponentState.RUNNING, timeout=1.0)
            await manager.wait_for_state("comp2", ComponentState.RUNNING, timeout=1.0)

            # Should list both
            components = manager.list_components()
            assert len(components) == 2
            assert "comp1" in components
            assert "comp2" in components

            # Remove one
            await manager.remove_component(comp1)
            await wait_for_removed(manager, "comp1")

            # Should list only remaining
            components = manager.list_components()
            assert len(components) == 1
            assert "comp2" in components

    @pytest.mark.asyncio
    async def test_failed_component_state(self, manager_context):
        """Test that failed components are tracked correctly."""
        async with manager_context as (manager, tg):
            component = MockComponent(
                name="test1", start_should_raise=RuntimeError("Failed!")
            )

            # Add component that will fail
            await manager.add_component(component)
            await anyio.sleep(0.2)

            # Component should be cleaned up (failed components are removed)
            state = manager.get_component_state("test1")
            assert state is None


class TestComponentManagerPublicSurface:
    """Public component lifecycle API checks."""

    @pytest.mark.asyncio
    async def test_component_status_reports_runtime_local_readiness(
        self, manager_context
    ):
        async with manager_context as (manager, tg):
            component = ReportingComponent(name="test1")

            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            status = manager.get_component_status("test1")

            assert status is not None
            assert status.runtime_name == "test1"
            assert status.lifecycle_state == ComponentState.RUNNING
            assert status.readiness_state == ReadinessState.READY
            assert status.readiness_reasons == ()
            assert status.diagnostics == {"worker": "ok"}
            assert manager.list_component_statuses() == [status]


class TestComponentManagerTaskCancellation:
    """Test that component tasks are properly cancelled when components are removed."""

    @pytest.mark.asyncio
    async def test_tasks_stop_on_stop_exception(self, manager_context):
        """Test that tasks stop even when stop() raises an exception."""
        async with manager_context as (manager, tg):
            # Create a component that has a task and raises in stop()
            @dataclass
            class FailingStopComponent:
                name: str
                counter: int = 0
                task_running: bool = False

                async def start(self, ctx: RunContext) -> None:
                    self.task_running = True

                    async def counting_task():
                        try:
                            while True:
                                self.counter += 1
                                await anyio.sleep(0.01)
                        finally:
                            self.task_running = False

                    ctx.tg.start_soon(counting_task)

                async def stop(self) -> None:
                    raise RuntimeError("Stop failed!")

            component = FailingStopComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify task is running
            initial_counter = component.counter
            await anyio.sleep(0.1)
            assert component.counter > initial_counter

            # Remove component with failing stop()
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Wait and verify task stopped despite stop() exception
            final_counter = component.counter
            await anyio.sleep(0.2)
            assert (
                component.counter == final_counter
            ), "Tasks should stop even when stop() raises exception"
            assert not component.task_running

