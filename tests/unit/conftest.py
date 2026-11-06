"""Shared fixtures: a fake Radiant API in place of the HTTP client."""

import asyncio
from contextlib import contextmanager
from typing import Any, Callable

import pytest
from radiant_python.exceptions import ApiException

from radiant_mcp.tools import _api

PORTAL = "https://portal.example"


class FakeApi:
    """Stands in for ``_api.RadiantApi``.

    ``responses[(api, operation)]`` is the JSON body to return, an exception
    to raise, or a callable taking the call's kwargs. Every call is recorded
    in ``calls`` as ``(api, operation, kwargs)``.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def on(self, api: str, operation: str, response: Any) -> "FakeApi":
        self.responses[(api, operation)] = response
        return self

    def call(self, api: str, operation: str, **kwargs) -> Any:
        self.calls.append((api, operation, kwargs))
        if (api, operation) not in self.responses:
            raise AssertionError(f"unexpected call {api}.{operation}({kwargs})")
        response = self.responses[(api, operation)]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(**kwargs)
        return response

    def operations(self) -> list[str]:
        return [operation for _, operation, _ in self.calls]

    def kwargs(self, operation: str) -> dict:
        return next(kwargs for _, op, kwargs in self.calls if op == operation)


def api_exception(status: int, body: str = "", headers: dict | None = None) -> ApiException:
    exc = ApiException(status=status, reason="error", body=body)
    exc.headers = headers or {}
    return exc


@pytest.fixture
def fake_api(monkeypatch) -> FakeApi:
    fake = FakeApi()

    @contextmanager
    def _connect(_token: str):
        yield fake

    monkeypatch.setattr(_api, "connect", _connect)
    monkeypatch.setattr(_api, "get_jwt_token", lambda: "jwt")
    monkeypatch.setenv("RADIANT_API_URL", "http://api.example")
    monkeypatch.setenv("RADIANT_PORTAL_URL", PORTAL)
    return fake


@pytest.fixture
def run() -> Callable:
    """Run a tool coroutine to completion."""
    return lambda coroutine: asyncio.run(coroutine)


def serialized(model) -> Any:
    """A request body as the HTTP client would send it."""
    import radiant_python

    client = radiant_python.ApiClient(radiant_python.Configuration(host="http://x"))
    return client.sanitize_for_serialization(model)
