"""CI-safe synchronous ASGI test client.

The sandbox used for this project cannot wake AnyIO's cross-thread portal or
thread pool. Starlette's TestClient therefore hangs before an endpoint runs.
This adapter keeps tests in-process and exercises the actual ASGI application,
but avoids those two AnyIO mechanisms *only while pytest is running*.
"""
import asyncio
import threading
from typing import Any

import httpx


async def run_sync_inline(func, *args, **kwargs):
    """Test-only replacement for AnyIO's unavailable worker thread pool."""
    return func(*args, **kwargs)


def install_test_runtime_compatibility() -> None:
    """Patch import-time aliases used for synchronous FastAPI endpoints."""
    import anyio.to_thread
    import starlette.background
    import fastapi.routing
    import starlette.concurrency

    starlette.concurrency.run_in_threadpool = run_sync_inline
    fastapi.routing.run_in_threadpool = run_sync_inline
    # ``BackgroundTask`` binds this helper at module import time.  API routes
    # that schedule work would otherwise still enter AnyIO's unavailable test
    # thread pool after the response body has been created.
    starlette.background.run_in_threadpool = run_sync_inline
    # Some Starlette/FastAPI internals retain their own aliases.  Patching the
    # source helper as well makes every synchronous route and BackgroundTask
    # follow the same in-process path during pytest.
    anyio.to_thread.run_sync = run_sync_inline


class ASGITestClient:
    """Small TestClient-compatible facade backed by HTTPX ASGITransport."""

    def __init__(self, app, base_url: str = "http://testserver", **kwargs: Any):
        self.app = app
        self.base_url = base_url
        self.headers = httpx.Headers(kwargs.pop("headers", None))
        self.cookies = kwargs.pop("cookies", None)
        self.follow_redirects = kwargs.pop("follow_redirects", True)
        self.raise_server_exceptions = kwargs.pop("raise_server_exceptions", True)
        self._lifespan = None
        self._runner: asyncio.Runner | None = None
        self._owner_thread: int | None = None

    async def _enter_lifespan(self) -> None:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()

    async def _exit_lifespan(self) -> None:
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)
            self._lifespan = None

    def __enter__(self):
        # A lifespan async generator must be entered and exited on the same
        # event loop.  ``asyncio.run`` creates a new loop for each call.
        self._runner = asyncio.Runner()
        self._runner.run(self._enter_lifespan())
        self._owner_thread = threading.get_ident()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self._runner is not None
        try:
            self._runner.run(self._exit_lifespan())
        finally:
            self._runner.close()
            self._runner = None
            self._owner_thread = None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = httpx.Headers(self.headers)
        headers.update(kwargs.pop("headers", None))

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(
                app=self.app, raise_app_exceptions=self.raise_server_exceptions,
            )
            async with httpx.AsyncClient(
                transport=transport, base_url=self.base_url, headers=headers,
                cookies=self.cookies, follow_redirects=self.follow_redirects,
            ) as client:
                return await client.request(method, url, **kwargs)

        if self._runner is not None and threading.get_ident() == self._owner_thread:
            return self._runner.run(send())
        # Concurrent test callers must not share an asyncio.Runner.  They
        # still execute the same ASGI application concurrently; only their
        # request-local event loop is independent of the fixture's lifespan
        # loop.
        return asyncio.run(send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)
