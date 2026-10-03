from unittest.mock import patch

import asyncio
import pytest

import run


def test_admin_port_is_busy_when_connection_succeeds():
    """A second local launch should stop before Uvicorn emits a bind traceback."""
    with patch("run.socket.create_connection") as connection:
        connection.return_value.__enter__.return_value = object()

        assert run.admin_port_is_busy() is True
        connection.assert_called_once_with(
            ("127.0.0.1", run.EnvKeys.ADMIN_PORT), timeout=0.25
        )


def test_admin_port_is_free_when_connection_fails():
    with patch("run.socket.create_connection", side_effect=OSError):
        assert run.admin_port_is_busy() is False


@pytest.mark.asyncio
async def test_admin_listener_failure_is_fatal_to_polling_loop():
    from bot.main import AppContext, _run_polling_with_admin_watch

    class DispatcherStub:
        async def start_polling(self, *_args, **_kwargs):
            await asyncio.sleep(60)

    async def admin_failure():
        await asyncio.sleep(0)
        raise RuntimeError("bind failed")

    ctx = AppContext(admin_server_task=asyncio.create_task(admin_failure()))
    with pytest.raises(RuntimeError, match="Admin listener failed"):
        await _run_polling_with_admin_watch(DispatcherStub(), object(), ctx)


@pytest.mark.asyncio
async def test_polling_return_does_not_leave_admin_task_running():
    from bot.main import AppContext, _run_polling_with_admin_watch

    class DispatcherStub:
        async def start_polling(self, *_args, **_kwargs):
            return None

    async def admin_running():
        await asyncio.sleep(60)

    admin_task = asyncio.create_task(admin_running())
    ctx = AppContext(admin_server_task=admin_task)
    await _run_polling_with_admin_watch(DispatcherStub(), object(), ctx)
    assert admin_task.cancelled()


@pytest.mark.asyncio
async def test_polling_passes_a_bounded_task_concurrency_limit():
    from bot.main import AppContext, _run_polling_with_admin_watch

    seen = {}

    class DispatcherStub:
        async def start_polling(self, *_args, **kwargs):
            seen.update(kwargs)

    await _run_polling_with_admin_watch(DispatcherStub(), object(), AppContext())

    assert seen["tasks_concurrency_limit"] == run.EnvKeys.POLLING_TASKS_CONCURRENCY
    assert seen["tasks_concurrency_limit"] > 0
