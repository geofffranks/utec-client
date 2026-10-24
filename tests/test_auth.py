"""Tests for AbstractAuth header injection."""

import aiohttp
import pytest
from aioresponses import aioresponses

from utec_client.auth import AbstractAuth


class _FakeAuth(AbstractAuth):
    def __init__(self, session, token="tok-123"):
        super().__init__(session)
        self._token = token

    async def async_get_access_token(self):
        return self._token


@pytest.mark.asyncio
async def test_headers_include_bearer_and_json_content_type():
    async with aiohttp.ClientSession() as session:
        auth = _FakeAuth(session)
        with aioresponses() as mock:
            mock.post("https://example.test/api", payload={"ok": True})
            resp = await auth.async_make_auth_request(
                "POST",
                "https://example.test/api",
                json={"hi": 1},
            )
            assert resp.status == 200

            call = mock.requests[("POST", __import__("yarl").URL("https://example.test/api"))][0]
            headers = call.kwargs["headers"]
            assert headers["authorization"] == "Bearer tok-123"
            assert headers["Content-Type"] == "application/json"
            assert headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_caller_headers_preserved_and_auth_overrides_nothing_except_auth_token():
    async with aiohttp.ClientSession() as session:
        auth = _FakeAuth(session)
        with aioresponses() as mock:
            mock.post("https://example.test/api", payload={})
            await auth.async_make_auth_request(
                "POST",
                "https://example.test/api",
                headers={"X-Request-Id": "req-42"},
                json={},
            )
            call = mock.requests[("POST", __import__("yarl").URL("https://example.test/api"))][0]
            headers = call.kwargs["headers"]
            assert headers["X-Request-Id"] == "req-42"
            assert headers["authorization"] == "Bearer tok-123"


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [list, tuple])
async def test_header_pair_iterables_preserve_custom_headers_and_replace_reserved(container):
    caller_headers = container([
        ("X-Request-Id", "req-42"),
        ("Authorization", "old-token"),
        ("content-type", "text/plain"),
        ("ACCEPT", "text/html"),
    ])
    original = container(caller_headers)
    async with aiohttp.ClientSession() as session:
        auth = _FakeAuth(session)
        with aioresponses() as mock:
            mock.post("https://example.test/api", payload={})
            await auth.async_make_auth_request(
                "POST", "https://example.test/api", headers=caller_headers
            )
            call = mock.requests[("POST", __import__("yarl").URL("https://example.test/api"))][0]
            headers = call.kwargs["headers"]
            assert headers == {
                "X-Request-Id": "req-42",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "authorization": "Bearer tok-123",
            }
            assert caller_headers == original


@pytest.mark.asyncio
async def test_mixed_case_caller_authorization_is_replaced_once():
    async with aiohttp.ClientSession() as session:
        auth = _FakeAuth(session)
        caller_headers = {"Authorization": "other-token", "X-Request-Id": "req-42"}
        with aioresponses() as mock:
            mock.post("https://example.test/api", payload={})
            await auth.async_make_auth_request(
                "POST", "https://example.test/api", headers=caller_headers
            )
            call = mock.requests[("POST", __import__("yarl").URL("https://example.test/api"))][0]
            headers = call.kwargs["headers"]
            auth_values = [
                value for name, value in headers.items() if name.lower() == "authorization"
            ]
            assert auth_values == ["Bearer tok-123"]
            assert headers["X-Request-Id"] == "req-42"
            assert caller_headers == {"Authorization": "other-token", "X-Request-Id": "req-42"}


@pytest.mark.asyncio
async def test_mixed_case_caller_json_headers_are_replaced_once():
    async with aiohttp.ClientSession() as session:
        auth = _FakeAuth(session)
        caller_headers = {"content-type": "text/plain", "ACCEPT": "text/html"}
        with aioresponses() as mock:
            mock.post("https://example.test/api", payload={})
            await auth.async_make_auth_request(
                "POST", "https://example.test/api", headers=caller_headers
            )
            call = mock.requests[("POST", __import__("yarl").URL("https://example.test/api"))][0]
            headers = call.kwargs["headers"]
            for name in ("content-type", "accept"):
                values = [value for key, value in headers.items() if key.lower() == name]
                assert values == ["application/json"]
            assert caller_headers == {"content-type": "text/plain", "ACCEPT": "text/html"}
