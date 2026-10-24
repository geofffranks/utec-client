"""Faithful behavior tests for aiohttp ``ClientResponse.release()``.

These tests establish what the installed aiohttp actually does instead of
assuming a contract from mocks. Each test exercises a real
``aiohttp.ClientResponse`` (created through aioresponses' public request
interception: real ``ClientResponse`` class with real release/cleanup code;
only the socket and event-loop stand-ins are inert) and observes the actual
side effects of ``release()``.

Verified against installed aiohttp 3.12.15 (project constraint
``aiohttp>=3.7.4,<4.0.0``; dev constraint ``<3.13``):

- ``ClientResponse.release()`` is a synchronous method: all connection
  cleanup (``_notify_content``, ``_cleanup_writer``,
  ``_release_connection``) runs during the call, before it returns.
- It returns ``aiohttp.helpers.noop()``, a trivial awaitable whose ``await``
  yields once and returns ``None``. It does NOT return ``None``.
- Both the original ``await response.release()`` and the current synchronous
  ``response.release()`` release the connection and preserve success/error
  outcomes; awaiting the result is optional cleanup.

These claims are version-bounded: older or newer releases could differ, and
these tests would catch such a dependency contract change.

aioresponses fixture note (aioresponses 0.7.9): its ``stop()`` closes every
built response, which sets the "Connection closed" stream exception and the
``_released`` flag — so response creation, body reads and release() must all
happen while the ``aioresponses()`` context is active.
"""

import inspect
import warnings

import aiohttp
import pytest
from aioresponses import aioresponses

from utec_client.api import UHomeApi
from utec_client.auth import AbstractAuth
from utec_client.const import API_BASE_URL
from utec_client.exceptions import ApiError


class _ReusableProtocol:
    """Matches the ``Connection.protocol`` surface aiohttp's ``read()`` checks in
    3.12.15: ``not protocol.upgraded`` marks a plain (non-WebSocket-upgraded)
    keep-alive connection, so ``read()`` treats the fully-read response as
    reusable and hands the connection back to the pool via real
    ``_wait_released``/``_release_connection`` code.
    """

    upgraded = False


class _PooledRecordingConnection:
    """Records aiohttp's real cleanup code returning the connection to the pool.

    Both ``ClientResponse._release_connection`` (from ``release()`` on an
    unread response) and ``ClientResponse.read()`` (on a fully-read response)
    call ``self._connection.release()`` in the reusable-connection case.
    """

    def __init__(self) -> None:
        self.release_count = 0
        self.protocol = _ReusableProtocol()

    def release(self) -> None:
        self.release_count += 1


async def _create_response(session, mock, *, status=200, payload=None, body=""):
    """Await a real ``aiohttp.ClientResponse`` via aioresponses' public API.

    Must be called while ``mock``'s context is active (see module docstring).
    """
    kwargs = {"status": status}
    if payload is not None:
        kwargs["payload"] = payload
    if body:
        kwargs["body"] = body
    mock.post(API_BASE_URL, **kwargs)
    resp = await session.post(API_BASE_URL)
    assert isinstance(resp, aiohttp.ClientResponse)
    return resp


# --- Dependency contract: what release() actually returns and does ---


@pytest.mark.asyncio
async def test_release_returns_awaitable_not_none(session):
    """aiohttp 3.12.15 release() returns a trivial awaitable (noop()), not None."""
    with aioresponses() as mock:
        resp = await _create_response(session, mock, payload={"ok": True})
        result = resp.release()
        assert result is not None
        assert inspect.isawaitable(result)
        assert not inspect.iscoroutine(result)  # an awaitable object, not a coroutine
        assert await result is None  # awaiting it adds nothing


@pytest.mark.asyncio
async def test_release_performs_cleanup_synchronously(session):
    """Cleanup runs during the sync call; dropping the returned awaitable is harmless.

    This is the pattern the production cleanup line uses: ``response.release()``
    without awaiting its result must still release the connection.
    """
    with aioresponses() as mock:
        resp = await _create_response(session, mock, payload={"ok": True})
        conn = _PooledRecordingConnection()
        resp._connection = conn
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = resp.release()
            del result  # never awaited
        assert conn.release_count == 1
        assert resp._connection is None  # real cleanup code detached the connection
        assert resp._released is True
        assert resp.closed


# --- Outcome preservation: original awaited cleanup vs current synchronous cleanup ---


async def _request_shape(resp, release_step):
    """Mirror of ``UHomeApi._async_make_request``'s outcome shape with the release
    step parameterized, so the original ``await response.release()`` and the
    current synchronous ``response.release()`` lines can be compared against real
    responses. Logging is omitted — not part of the outcome contract.
    """
    try:
        if resp.status == 204:
            return {}
        if resp.status in (200, 201, 202):
            return await resp.json()
        error_text = await resp.text()
        raise ApiError(resp.status, error_text)
    finally:
        await release_step(resp)


async def _release_awaited(resp):
    """The original branch's cleanup line: ``await response.release()``."""
    return await resp.release()


async def _release_sync(resp):
    """The current branch's cleanup line: ``response.release()``."""
    return resp.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 204, 400])
@pytest.mark.parametrize(
    "release_step",
    [_release_awaited, _release_sync],
    ids=["original-await-response-release", "current-sync-response-release"],
)
async def test_request_outcome_preserved_with_real_response(session, release_step, status):
    """Both cleanup lines release the connection and preserve the outcome.

    Success payloads, the 204 empty body and the HTTP error path must come out
    identically whether the release line is awaited (original) or called
    synchronously (current).
    """
    with aioresponses() as mock:
        resp = await _create_response(
            session,
            mock,
            status=status,
            payload={"devices": []} if status in (200, 201, 202) else None,
            body="Bad Request" if status == 400 else "",
        )
        conn = _PooledRecordingConnection()
        resp._connection = conn
        if status == 400:
            with pytest.raises(ApiError) as exc:
                await _request_shape(resp, release_step)
            assert exc.value.status_code == 400
        else:
            result = await _request_shape(resp, release_step)
            assert result == ({} if status == 204 else {"devices": []})
        assert conn.release_count == 1
        assert resp._connection is None
        assert resp.closed


# --- Production cleanup against real responses ---


class _CapturingAuth(AbstractAuth):
    """Auth that attaches a recording connection to the real ClientResponse it
    returns, so the production ``finally`` cleanup is observable.
    """

    def __init__(self, websession) -> None:
        super().__init__(websession)
        self.last_response = None
        self.connection = _PooledRecordingConnection()

    async def async_get_access_token(self) -> str:
        return "tok"

    async def async_make_auth_request(self, method, host, **kwargs):
        resp = await super().async_make_auth_request(method, host, **kwargs)
        resp._connection = self.connection
        self.last_response = resp
        return resp


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 204, 400])
async def test_production_sync_release_cleans_up_real_response(session, status):
    """``UHomeApi._async_make_request``'s current synchronous cleanup releases the
    real aiohttp response it gets, including the error path where the exception
    propagates through the ``finally`` block.
    """
    auth = _CapturingAuth(session)
    api = UHomeApi(auth)
    with aioresponses() as mock:
        if status == 400:
            mock.post(API_BASE_URL, status=400, body="Bad Request")
            with pytest.raises(ApiError) as exc:
                await api._async_make_request()
            assert exc.value.status_code == 400
        else:
            mock.post(API_BASE_URL, status=status, payload={"devices": []})
            result = await api._async_make_request()
            assert result == ({} if status == 204 else {"devices": []})
        assert auth.connection.release_count == 1
        assert auth.last_response._connection is None
        assert auth.last_response.closed
