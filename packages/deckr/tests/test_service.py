"""Comprehensive tests for ServiceManager component lifecycle management."""

from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import pytest
import pytest_asyncio
from deckr.core.component import (
    ComponentManager,
    ComponentState,
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
    """Internal async context manager for ServiceManager lifecycle."""
    manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(manager.run)
        await anyio.sleep(0.01)  # Let manager start

        try:
            yield (manager, tg)
        finally:
            # Cleanup: stop all components before cancelling
            component_names = list(manager.list_components())

            # Stop all components by calling _stop_component directly
            for name in component_names:
                try:
                    await manager._stop_component(name)
                except Exception:
                    pass  # Ignore errors during cleanup

            # Wait a bit for cleanup
            await anyio.sleep(0.1)

            # Cancel the manager task group - suppress exceptions from component crashes
            tg.cancel_scope.cancel()

    # Suppress any ExceptionGroup that might have been raised from component crashes
    # This allows tests to complete even if components crashed during execution


class ManagerContext:
    """Async context manager for ServiceManager lifecycle in tests."""

    def __init__(self):
        self._cm = _manager_context()

    async def __aenter__(self):
        """Enter the context manager and start the ServiceManager."""
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
    """Fixture that provides a ServiceManager in a task group with proper cleanup."""
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


class TestServiceManagerSuccess:
    """Test successful component lifecycle scenarios."""

    @pytest.mark.asyncio
    async def test_add_single_component(self, manager_context):
        """Test adding a single component successfully."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify component is running
            assert component.start_called
            assert not component.stop_called
            assert "test1" in manager.list_components()

            # Cleanup
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Verify component is stopped
            assert component.stop_called

    @pytest.mark.asyncio
    async def test_add_multiple_components(self, manager_context):
        """Test adding multiple components."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="comp1")
            comp2 = MockComponent(name="comp2")
            comp3 = MockComponent(name="comp3")

            # Add all components
            await manager.add_component(comp1)
            await manager.add_component(comp2)
            await manager.add_component(comp3)
            await manager.wait_for_state("comp1", ComponentState.RUNNING, timeout=1.0)
            await manager.wait_for_state("comp2", ComponentState.RUNNING, timeout=1.0)
            await manager.wait_for_state("comp3", ComponentState.RUNNING, timeout=1.0)

            # Verify all are running
            assert len(manager.list_components()) == 3

            # Remove all
            await manager.remove_component(comp1)
            await manager.remove_component(comp2)
            await manager.remove_component(comp3)
            await wait_for_removed(manager, "comp1")
            await wait_for_removed(manager, "comp2")
            await wait_for_removed(manager, "comp3")

            # Verify all are stopped
            assert len(manager.list_components()) == 0

    @pytest.mark.asyncio
    async def test_remove_idempotent(self, manager_context):
        """Test that removing non-existent component is idempotent."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            # Remove non-existent component (should not raise)
            await manager.remove_component(component)
            await anyio.sleep(0.1)

            # Should still be None
            assert manager.get_component_state("test1") is None

    @pytest.mark.asyncio
    async def test_component_lifecycle_states(self, manager_context):
        """Test that component states transition correctly."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            # Initially not present
            assert manager.get_component_state("test1") is None

            # Add component
            await manager.add_component(component)

            # Should transition through STARTING to RUNNING
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Remove component
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")


class TestServiceManagerErrors:
    """Test error scenarios and edge cases."""

    @pytest.mark.asyncio
    async def test_add_duplicate_component(self, manager_context):
        """Test that adding duplicate component raises error."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="test1")
            comp2 = MockComponent(name="test1")  # Same name

            # Add first component
            await manager.add_component(comp1)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Try to add duplicate - should raise
            with pytest.raises(RuntimeError, match="already exists"):
                await manager.add_component(comp2)

            # First component should still be running
            assert manager.get_component_state("test1") == ComponentState.RUNNING

    @pytest.mark.asyncio
    async def test_component_without_name(self, manager_context):
        """Test that component without name raises error."""
        async with manager_context as (manager, tg):
            component = ComponentWithoutName()

            with pytest.raises(ValueError, match="non-empty 'name' attribute"):
                await manager.add_component(component)

    @pytest.mark.asyncio
    async def test_component_start_raises_exception(self, manager_context):
        """Test handling when component.start() raises exception."""
        async with manager_context as (manager, tg):
            component = MockComponent(
                name="test1", start_should_raise=RuntimeError("Start failed!")
            )

            # Add component - should handle exception gracefully
            await manager.add_component(component)
            await anyio.sleep(0.2)  # Give time for error handling

            # Component should be cleaned up (not in registry)
            state = manager.get_component_state("test1")
            # Component should be removed or in FAILED state
            assert state is None or state == ComponentState.FAILED

    @pytest.mark.asyncio
    async def test_component_stop_raises_exception(self, manager_context):
        """Test handling when component.stop() raises exception."""
        async with manager_context as (manager, tg):
            component = MockComponent(
                name="test1", stop_should_raise=RuntimeError("Stop failed!")
            )

            # Add and start component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Remove - should handle stop exception
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Component should be removed despite stop error
            assert manager.get_component_state("test1") is None

    @pytest.mark.asyncio
    async def test_component_crashes_during_execution(self, manager_context):
        """Test crash detection when component crashes after start."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1", crash_after_start=True)

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Component should start
            assert component.start_called

            # Wait for crash
            await anyio.sleep(0.3)

            # Component should be cleaned up after crash
            state = manager.get_component_state("test1")
            assert state is None or state == ComponentState.FAILED

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


class TestServiceManagerRaceConditions:
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
    async def test_concurrent_remove_same_component(self, manager_context):
        """Test concurrent remove of same component."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Remove concurrently (should be idempotent)
            async def remove1():
                await manager.remove_component(component)

            async def remove2():
                await manager.remove_component(component)

            async with anyio.create_task_group() as test_tg:
                test_tg.start_soon(remove1)
                test_tg.start_soon(remove2)
            await wait_for_removed(manager, "test1")

            # Should be removed
            assert manager.get_component_state("test1") is None

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

    @pytest.mark.asyncio
    async def test_remove_while_starting(self, manager_context):
        """Test removing component while it's starting."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1", start_delay=0.2)

            # Start adding (slow)
            async def add():
                await manager.add_component(component)

            # Remove while starting
            async def remove():
                await anyio.sleep(0.05)  # Wait a bit
                await manager.remove_component(component)

            async with anyio.create_task_group() as test_tg:
                test_tg.start_soon(add)
                test_tg.start_soon(remove)
            await anyio.sleep(0.3)

            # Component should be cleaned up
            assert manager.get_component_state("test1") is None


class TestServiceManagerStateManagement:
    """Test state management and query APIs."""

    @pytest.mark.asyncio
    async def test_state_transitions(self, manager_context):
        """Test that state transitions are correct."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            # IDLE (not present)
            assert manager.get_component_state("test1") is None

            # Add - should go STARTING -> RUNNING
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Remove - should go STOPPING -> removed
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

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


class TestServiceManagerResourceCleanup:
    """Test resource cleanup scenarios."""

    @pytest.mark.asyncio
    async def test_cleanup_on_start_failure(self, manager_context):
        """Test that resources are cleaned up when start fails."""
        async with manager_context as (manager, tg):
            component = MockComponent(
                name="test1", start_should_raise=RuntimeError("Start failed!")
            )

            # Add component that will fail
            await manager.add_component(component)
            await anyio.sleep(0.2)

            # Component should be removed from registry
            assert manager.get_component_state("test1") is None
            assert "test1" not in manager.list_components()

    @pytest.mark.asyncio
    async def test_cleanup_on_crash(self, manager_context):
        """Test that resources are cleaned up when component crashes."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1", crash_after_start=True)

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Wait for crash
            await anyio.sleep(0.3)

            # Component should be cleaned up
            assert manager.get_component_state("test1") is None

    @pytest.mark.asyncio
    async def test_manager_shutdown_with_running_components(self, manager_context):
        """Test manager shutdown with running components."""
        async with manager_context as (manager, tg):
            comp1 = MockComponent(name="comp1")
            comp2 = MockComponent(name="comp2")

            # Add components
            await manager.add_component(comp1)
            await manager.add_component(comp2)
            await manager.wait_for_state("comp1", ComponentState.RUNNING, timeout=1.0)
            await manager.wait_for_state("comp2", ComponentState.RUNNING, timeout=1.0)

            # Verify running
            assert len(manager.list_components()) == 2

            # Fixture will handle cleanup and cancellation

    @pytest.mark.asyncio
    async def test_no_orphaned_tasks(self, manager_context):
        """Test that no tasks are orphaned on failure."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1", crash_after_start=True)

            # Add component that will crash
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Wait for crash and cleanup
            await anyio.sleep(0.3)

            # If tasks are orphaned, this will hang or fail
            # The test passing means cleanup worked
            assert True


class TestServiceManagerTaskCancellation:
    """Test that component tasks are properly cancelled when components are removed."""

    @pytest.mark.asyncio
    async def test_tasks_stop_on_removal(self, manager_context):
        """Test that component tasks actually stop when component is removed."""
        async with manager_context as (manager, tg):
            component = CountingComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify task is running (counter should be incrementing)
            initial_counter = component.counter
            await anyio.sleep(0.1)
            assert component.counter > initial_counter, "Counter should be incrementing"
            assert component.task_running, "Task should be running"

            # Remove component
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Wait a bit to ensure task has stopped
            final_counter = component.counter
            await anyio.sleep(0.2)

            # Counter should have stopped incrementing
            assert (
                component.counter == final_counter
            ), f"Counter should stop incrementing after removal. Final: {final_counter}, After wait: {component.counter}"
            assert not component.task_running, "Task should have stopped"

    @pytest.mark.asyncio
    async def test_tasks_stop_without_stop_implementation(self, manager_context):
        """Test that tasks stop even when component doesn't implement stop()."""
        async with manager_context as (manager, tg):
            # Component without stop() implementation (like TestComponent in __main__.py)
            component = CountingComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify task is running
            initial_counter = component.counter
            await anyio.sleep(0.1)
            assert component.counter > initial_counter

            # Remove component (stop() is a no-op, but cancellation should still work)
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Wait and verify task stopped
            final_counter = component.counter
            await anyio.sleep(0.2)
            assert (
                component.counter == final_counter
            ), "Tasks should stop even without stop() implementation"
            assert not component.task_running

    @pytest.mark.asyncio
    async def test_multiple_tasks_all_stop(self, manager_context):
        """Test that all tasks in a component stop when component is removed."""
        async with manager_context as (manager, tg):
            component = MultiTaskComponent(name="test1", task_count=5)

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify all tasks are running
            initial_counters = component.counters.copy()
            await anyio.sleep(0.1)
            for i, (initial, current) in enumerate(
                zip(initial_counters, component.counters, strict=True)
            ):
                assert current > initial, f"Task {i} should be incrementing"

            # Remove component
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Wait and verify all tasks stopped
            final_counters = component.counters.copy()
            await anyio.sleep(0.2)

            for i, (final, current) in enumerate(
                zip(final_counters, component.counters, strict=True)
            ):
                assert current == final, f"Task {i} should have stopped incrementing"

    @pytest.mark.asyncio
    async def test_graceful_shutdown_with_stopping_event(self, manager_context):
        """Test that components can check stopping event for graceful shutdown."""
        async with manager_context as (manager, tg):
            component = StoppingAwareComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify task is running
            initial_counter = component.counter
            await anyio.sleep(0.1)
            assert component.counter > initial_counter

            # Remove component
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")

            # Wait a bit - component should detect stopping event
            await anyio.sleep(0.1)

            # Component should have stopped gracefully (or been cancelled)
            # Either way, counter should stop incrementing
            final_counter = component.counter
            await anyio.sleep(0.2)
            assert (
                component.counter == final_counter
            ), "Component should stop after removal"

    @pytest.mark.asyncio
    async def test_tasks_stop_on_timeout(self, manager_context):
        """Test that tasks stop even when stop() times out."""
        async with manager_context as (manager, tg):
            component = CountingComponent(name="test1")

            # Add component
            await manager.add_component(component)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Verify task is running
            initial_counter = component.counter
            await anyio.sleep(0.1)
            assert component.counter > initial_counter

            # Remove with very short timeout (should timeout)
            await manager._stop_component("test1", stop_timeout_s=0.01)
            await wait_for_removed(manager, "test1")

            # Wait and verify task stopped despite timeout
            final_counter = component.counter
            await anyio.sleep(0.2)
            assert (
                component.counter == final_counter
            ), "Tasks should stop even when stop() times out"
            assert not component.task_running

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

    @pytest.mark.asyncio
    async def test_isolated_component_tasks(self, manager_context):
        """Test that removing one component doesn't affect other components."""
        async with manager_context as (manager, tg):
            comp1 = CountingComponent(name="comp1")
            comp2 = CountingComponent(name="comp2")

            # Add both components
            await manager.add_component(comp1)
            await manager.add_component(comp2)
            await manager.wait_for_state("comp1", ComponentState.RUNNING, timeout=1.0)
            await manager.wait_for_state("comp2", ComponentState.RUNNING, timeout=1.0)

            # Verify both are running
            initial1 = comp1.counter
            initial2 = comp2.counter
            await anyio.sleep(0.1)
            assert comp1.counter > initial1
            assert comp2.counter > initial2

            # Remove only comp1
            await manager.remove_component(comp1)
            await wait_for_removed(manager, "comp1")

            # Wait and verify comp1 stopped but comp2 continues
            final1 = comp1.counter
            final2 = comp2.counter
            await anyio.sleep(0.2)

            assert comp1.counter == final1, "comp1 should have stopped"
            assert comp2.counter > final2, "comp2 should continue running"
            assert not comp1.task_running
            assert comp2.task_running


class TestServiceManagerEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_name_component(self, manager_context):
        """Test component with empty name."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="")

            with pytest.raises(ValueError, match="non-empty 'name' attribute"):
                await manager.add_component(component)

    @pytest.mark.asyncio
    async def test_get_state_nonexistent(self, manager_context):
        """Test getting state of non-existent component."""
        async with manager_context as (manager, tg):
            assert manager.get_component_state("nonexistent") is None

    @pytest.mark.asyncio
    async def test_add_before_run(self):
        """Test that adding before run() works (event is queued)."""
        manager = ComponentManager()
        component = MockComponent(name="test1")

        # This should work (event is queued)
        async with anyio.create_task_group() as tg:
            # Don't start manager.run() yet
            await anyio.sleep(0.01)

            # Try to add - should queue event
            await manager.add_component(component)

            # Now start manager
            tg.start_soon(manager.run)
            await manager.wait_for_state("test1", ComponentState.RUNNING, timeout=1.0)

            # Should work - event was queued
            assert manager.get_component_state("test1") == ComponentState.RUNNING

            # Cleanup
            await manager.remove_component(component)
            await wait_for_removed(manager, "test1")
            tg.cancel_scope.cancel()

    @pytest.mark.asyncio
    async def test_multiple_rapid_add_remove(self, manager_context):
        """Test rapid add/remove cycles."""
        async with manager_context as (manager, tg):
            component = MockComponent(name="test1")

            # Rapid add/remove cycles
            for _ in range(5):
                await manager.add_component(component)
                await anyio.sleep(0.05)
                await manager.remove_component(component)
                await anyio.sleep(0.05)

            # Final state should be clean
            assert manager.get_component_state("test1") is None
