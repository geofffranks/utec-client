"""Exercise version discovery through HTTP decoding and the executable boundary."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pypi_preflight as pp

ROOT = Path(__file__).resolve().parents[1]


def run_preflight(tmp_path, responses):
    """Inject only urlopen; run the real script, parser and JSON decoder."""
    bootstrap = """
import io, json, os, runpy, urllib.error, urllib.request
responses = iter(json.loads(os.environ['PYPI_RESPONSES']))
def urlopen(url, timeout):
    item = next(responses)
    if item == 'timeout':
        raise TimeoutError('lookup timed out')
    if item == 'transport':
        raise urllib.error.URLError('connection lost')
    if item == 'unexpected':
        raise RuntimeError('unexpected lookup failure')
    if isinstance(item, int):
        raise urllib.error.HTTPError(url, item, 'HTTP failure', {}, None)
    return io.BytesIO(item.encode())
urllib.request.urlopen = urlopen
runpy.run_path('scripts/pypi_preflight.py', run_name='__main__')
"""
    return subprocess.run(
        [sys.executable, "-c", bootstrap, "--check-files-exist", "--version", "1.2.3"],
        cwd=ROOT,
        env={**os.environ, "PYPI_RESPONSES": json.dumps(responses)},
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "payload",
    [
        "null",
        "[]",
        "true",
        "0",
        '"unknown"',
        "{}",
        '{"urls": null}',
        '{"urls": {}}',
        '{"urls": "unknown"}',
        "{broken",
    ],
)
def test_successful_malformed_version_is_not_absence(tmp_path, payload):
    result = run_preflight(tmp_path, [payload])
    assert result.returncode == 4
    assert not result.stdout
    assert "unknown state" in result.stderr


@pytest.mark.parametrize("failure", [403, 500, "transport", "timeout", "unexpected"])
@pytest.mark.parametrize("secondary", [False, True])
def test_primary_and_secondary_errors_never_authorize_build(tmp_path, failure, secondary):
    responses = [404, failure] if secondary else [failure]
    result = run_preflight(tmp_path, responses)
    assert result.returncode == 4
    assert not result.stdout
    assert "cannot determine PyPI state" in result.stderr


@pytest.mark.parametrize("payload", ["null", "{}", "[]", '{"urls": null}', "broken"])
def test_secondary_malformed_json_is_unknown(tmp_path, payload):
    result = run_preflight(tmp_path, [404, payload])
    assert result.returncode == 4
    assert not result.stdout


@pytest.mark.parametrize(
    ("responses", "code", "detail"),
    [
        ([404, 404], 1, "project-not-on-pypi"),
        ([404, '{"urls": [{"filename": "older.whl"}]}'], 1, "version-not-published"),
        (['{"urls": []}'], 1, "version-published"),
        (['{"urls": [{"filename": "selected.whl"}]}'], 0, "version-published"),
    ],
)
def test_confirmed_version_outcomes(tmp_path, responses, code, detail):
    result = run_preflight(tmp_path, responses)
    assert result.returncode == code, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "project": "utec-client",
        "version": "1.2.3",
        "has_files": code == 0,
        "detail": detail,
    }


def test_fetch_null_cannot_be_confused_with_404(monkeypatch):
    monkeypatch.setattr(pp.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"null"))
    with pytest.raises(ValueError, match="version response"):
        pp.fetch_pypi_version("utec-client", "1.2.3")


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_mode_boundary_preserves_termination(monkeypatch, failure):
    def stop(args):
        raise failure()

    monkeypatch.setattr(pp, "cmd_check_files_exist", stop)
    with pytest.raises(failure):
        pp.main(["--check-files-exist", "--version", "1.2.3"])


def test_usage_error_is_not_absence():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/pypi_preflight.py"), "--check-files-exist"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "requires --version" in result.stderr
