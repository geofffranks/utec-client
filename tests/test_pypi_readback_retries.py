"""Bounded post-upload verification without uploads or real waiting."""

import argparse
import urllib.error
from unittest.mock import Mock

import pytest

import pypi_preflight as pp


@pytest.fixture
def readback(monkeypatch):
    monkeypatch.setattr(pp, "local_dists", lambda _: {"release.whl": "abc"})
    pause = Mock()
    monkeypatch.setattr(pp.time, "sleep", pause)
    args = argparse.Namespace(
        dist_dir="dist", project="utec-client", readback_attempts=3, readback_interval=10
    )
    return args, pause


def test_delayed_visibility(readback, monkeypatch):
    args, pause = readback
    fetch = Mock(side_effect=[{}, {}, {"release.whl": {"digests": {"sha256": "abc"}}}])
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 0
    assert fetch.call_count == 3
    assert pause.call_count == 2
    pause.assert_called_with(10)


def test_missing_files_exhaust_retries(readback, monkeypatch):
    args, pause = readback
    fetch = Mock(return_value={})
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 1
    assert fetch.call_count == 3
    assert pause.call_count == 2


def test_digest_conflict_never_retries(readback, monkeypatch):
    args, pause = readback
    fetch = Mock(return_value={"release.whl": {"digests": {"sha256": "wrong"}}})
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 1
    fetch.assert_called_once()
    pause.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("connection failed"),
        TimeoutError("timed out"),
        urllib.error.HTTPError("url", 429, "rate limited", {}, None),
        urllib.error.HTTPError("url", 503, "unavailable", {}, None),
    ],
)
def test_transient_errors_recover(readback, monkeypatch, error):
    args, pause = readback
    fetch = Mock(side_effect=[error, {"release.whl": {"digests": {"sha256": "abc"}}}])
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 0
    assert fetch.call_count == 2
    pause.assert_called_once_with(10)


def test_transient_errors_exhaust_retries(readback, monkeypatch):
    args, pause = readback
    fetch = Mock(side_effect=urllib.error.URLError("connection failed"))
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 1
    assert fetch.call_count == 3
    assert pause.call_count == 2


def test_permanent_http_error_never_retries(readback, monkeypatch):
    args, pause = readback
    fetch = Mock(side_effect=urllib.error.HTTPError("url", 403, "forbidden", {}, None))
    monkeypatch.setattr(pp, "fetch_pypi", fetch)
    assert pp.cmd_verify_published(args) == 1
    fetch.assert_called_once()
    pause.assert_not_called()


@pytest.mark.parametrize(
    "flags",
    [
        ["--readback-attempts", "0"],
        ["--readback-interval", "-1"],
    ],
)
def test_invalid_retry_arguments(flags):
    with pytest.raises(SystemExit) as exc:
        pp.main(["--verify-published", *flags])
    assert exc.value.code == 2
