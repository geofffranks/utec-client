"""Fail-closed release-discovery tests: real finder CLI + real publish.yml blocks.

Complements tests/test_publish_pipeline.py (which drives the finder as a
module): everything here executes the actual CLI (`sys.executable
scripts/find_validated_build.py`) or the actual workflow step run bodies
extracted from .github/workflows/publish.yml, with a controlled `gh` stub
serving recorded API pages and, for the gate composition, a staged
pypi_preflight exit code. No decision logic is re-simulated here.

Covers the release-safety requirements:
- exit 0/1/2 contract unchanged: reuse tuple on stdout, completed scan with
  no candidate is 1, expired artifact defaults to 2 (an entirely absent
  artifact is still "no candidate", not unusable)
- unknown state (gh failure, invalid JSON, non-object envelope, wrong
  endpoint items key, malformed items, missing or mistyped decision fields,
  later-page failures, unexpected operational errors) exits 4 with
  actionable stderr — never silently "no candidate"; in particular [{}]
  items on any endpoint and artifacts whose expired flag is missing/null/
  non-boolean can never become "no candidate" or "not expired", while
  legitimate nonmatches (pull_request events, other workflows, null head
  branches, pending jobs) are preserved
- usage errors exit 2 and are never remapped; reserved/out-of-range
  --unusable-artifact-code values are rejected via parser.error while other
  custom codes keep working
- the workflow decide step writes outputs ONLY for exit 0 (reuse=true) and
  exit 1 (reused=false); 2/4/unexpected fail the step before any output
- only "no candidate" reaches the independent PyPI rebuild gate; unknown or
  unexpected discovery states block the gate even when PyPI is empty
- workflow-block harnesses run with an isolated bin as the ONLY PATH entry
  (symlinked awk/sed plus python/gh stubs; bash by absolute path), so an
  unlaunchable stub gh cannot fall through exec resolution to a real host
  gh — pinned by a dedicated resolution regression
- interrupts (KeyboardInterrupt/SystemExit) are not swallowed by the exit-4
  boundary (it catches Exception, not BaseException)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

import pytest

import find_validated_build as fv

REPO_ROOT = Path(__file__).resolve().parent.parent
FINDER = REPO_ROOT / "scripts" / "find_validated_build.py"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"

# Absolute path: harness scripts run with an isolated PATH that has no bash.
BASH = shutil.which("bash")
assert BASH, "bash not found on host"

SHA = "a" * 40
TAG = "v1.0.0"
VERSION = "1.0.0"
ARTIFACT = f"release-dists-{SHA}"

RUNS = "repos/geofffranks/utec-py/actions/runs"
ENDPOINTS = {
    "runs": RUNS,
    "jobs": "repos/geofffranks/utec-py/actions/runs/42/jobs",
    "artifacts": "repos/geofffranks/utec-py/actions/runs/42/artifacts",
}
EXPECTED_KEY = {"runs": "workflow_runs", "jobs": "jobs", "artifacts": "artifacts"}

DECIDE_MARKER = "id: decide"
GATE_MARKER = "name: Rebuilding this version is only allowed"

# Actions runner templates the local bash harness must pre-expand; the decide
# block takes its inputs from step env, but the gate block inlines one.
RUNNER_TEMPLATE_EXPANSIONS = {
    "${{ steps.prepub.outputs.sha }}": "${SOURCE_SHA}",
}

GH_STUB_TEMPLATE = '''#!@INTERPRETER@
"""Controlled gh stub: serves recorded API pages per FAKE_GH_ROUTES."""
import json
import os
import sys

argv = sys.argv[1:]
if len(argv) != 2 or argv[0] != "api":
    sys.stderr.write("fake gh: expected 'gh api <url>'; got %r\\n" % (argv,))
    sys.exit(64)
base, _, query = argv[1].partition("?")
params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
routes = json.load(open(os.environ["FAKE_GH_ROUTES"]))
spec = routes.get(base)
if spec is None:
    sys.stderr.write("fake gh: no route recorded for %s\\n" % base)
    sys.exit(66)
page = int(params.get("page", "1"))
if page < 1 or page > len(spec):
    sys.stderr.write("fake gh: no route for page %d of %s\\n" % (page, base))
    sys.exit(65)
resp = spec[page - 1]
if "stderr" in resp:
    sys.stderr.write(resp["stderr"])
if "raw" in resp:
    sys.stdout.write(resp["raw"])
elif "body" in resp:
    sys.stdout.write(json.dumps(resp["body"]))
sys.exit(resp.get("code", 0))
'''

# Passes every invocation to the real interpreter, except the PyPI gate
# script, whose exit code is staged (its own semantics are covered elsewhere).
PYTHON_STUB_TEMPLATE = """#!@INTERPRETER@
import os
import sys

args = sys.argv[1:]
if args and args[0].endswith("pypi_preflight.py"):
    with open(os.environ["PREFLIGHT_CALLS"], "a") as fh:
        fh.write(" ".join(args) + "\\n")
    sys.exit(int(os.environ["FAKE_PREFLIGHT_CODE"]))
os.execv(sys.executable, [sys.executable] + args)
"""


class GhControl:
    """Writes a controlled `gh` stub plus its recorded routes into a bin dir."""

    def __init__(self, workspace: Path):
        self.bin = workspace / "bin"
        self.bin.mkdir()
        self.routes_path = workspace / "gh_routes.json"
        self._routes: dict = {}
        self._write()
        self._install("gh", GH_STUB_TEMPLATE)
        self._install("python", PYTHON_STUB_TEMPLATE)

    def _install(self, name: str, template: str) -> None:
        path = self.bin / name
        path.write_text(template.replace("@INTERPRETER@", sys.executable))
        path.chmod(0o755)

    def _write(self) -> None:
        self.routes_path.write_text(json.dumps(self._routes))

    def route(self, base: str, *pages: dict) -> None:
        """Record the page responses for one API URL (page 1 first)."""
        self._routes[base] = list(pages)
        self._write()

    def isolated_bin(self, *, break_gh: bool = False) -> Path:
        """Build a PATH-isolated bin: stubs + explicitly symlinked utilities.

        The workflow run blocks need only bash builtins (set/echo/test/exit),
        the `python` stub, a stub (or broken) `gh`, and awk/sed. Everything
        reachable is linked in here explicitly, and tests use it as the ONLY
        PATH entry: an unlaunchable stub gh then cannot fall through exec
        resolution (ENOENT) to a real host gh such as /usr/bin/gh on Linux or
        /opt/homebrew/bin/gh here. Invoke bash by absolute path.
        """
        iso = self.bin.parent / "isolated_bin"
        iso.mkdir(exist_ok=True)
        for util in ("awk", "sed"):
            resolved = shutil.which(util)
            assert resolved, f"required utility {util!r} not found on host"
            link = iso / util
            if not link.exists():
                link.symlink_to(resolved)
        stub_link = iso / "python"
        if not stub_link.exists():
            stub_link.symlink_to(self.bin / "python")
        gh_link = iso / "gh"
        if break_gh:
            # Write a fresh file (never write through the symlink).
            gh_link.unlink(missing_ok=True)
            gh_link.write_text("#!/nonexistent-interpreter-for-tests\n")
            gh_link.chmod(0o755)
        elif not gh_link.exists():
            gh_link.symlink_to(self.bin / "gh")
        return iso

    def env(self, path: str | None = None, **extra: str) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}" if path is None else path
        env["GH_REPO"] = "geofffranks/utec-py"
        env["FAKE_GH_ROUTES"] = str(self.routes_path)
        env.update(extra)
        return env


@pytest.fixture
def workspace(tmp_path):
    """Per-test scratch dir; honors POLYTOKEN_DELIVERY_SCRATCH_DIR when set."""
    base = os.environ.get("POLYTOKEN_DELIVERY_SCRATCH_DIR")
    if not base:
        yield tmp_path
        return
    scratch = Path(base) / "release-discovery-tests"
    scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def gh(workspace):
    return GhControl(workspace)


def envelope(key: str, items: list, total: int | None = None) -> dict:
    return {
        "code": 0,
        "body": {"total_count": len(items) if total is None else total, key: items},
    }


def gh_fail(stderr: str = "gh: HTTP 502 (retries exhausted)", code: int = 1) -> dict:
    return {"code": code, "stderr": stderr}


def trusted_run(run_id: int, *, num: int = 1) -> dict:
    """A trusted publish.yml run whose build job succeeded, publish failed."""
    return {
        "id": run_id,
        "run_number": num,
        "head_branch": "main",
        "conclusion": "failure",  # whole run failed at publish time
        "path": fv.TRUSTED_WORKFLOW_PATH,
        "event": "workflow_dispatch",
    }


def build_job() -> dict:
    return {"name": "build", "conclusion": "success"}


def good_artifact() -> dict:
    return {"name": ARTIFACT, "expired": False}


def route_candidate(gh: GhControl) -> None:
    """Record a full reusable-candidate scenario (override endpoints after)."""
    gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
    gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
    gh.route(ENDPOINTS["artifacts"], envelope("artifacts", [good_artifact()]))


def run_finder(env: dict, *args: str) -> subprocess.CompletedProcess:
    """Execute the real finder CLI."""
    return subprocess.run(
        [sys.executable, str(FINDER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


def harness_env(gh: GhControl, workspace: Path, path: str | None = None, **extra: str) -> dict:
    env = gh.env(
        path=path,
        GITHUB_OUTPUT=str(workspace / "github_output"),
        PREFLIGHT_CALLS=str(workspace / "preflight_calls.log"),
        SOURCE_SHA=SHA,
        CURRENT_RUN="999",
        RELEASE_TAG=TAG,
        RELEASE_VERSION=VERSION,
    )
    env.update(extra)
    return env


def iso_env(gh: GhControl, workspace: Path, *, break_gh: bool = False, **extra: str) -> dict:
    """Harness env whose PATH is the isolated bin ONLY (no host fallthrough)."""
    return harness_env(gh, workspace, path=str(gh.isolated_bin(break_gh=break_gh)), **extra)


def outputs_of(env: dict) -> dict:
    path = Path(env["GITHUB_OUTPUT"])
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def run_decide_step(
    gh: GhControl, workspace: Path, decide_block: str, env: dict
) -> subprocess.CompletedProcess:
    """Execute the actual decide run block with GITHUB_OUTPUT staging."""
    script = workspace / "decide_step.sh"
    script.write_text('#!/bin/bash\nset -u\n: > "$GITHUB_OUTPUT"\n' + decide_block)
    return subprocess.run(
        [BASH, str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


def run_decide_then_gate(
    gh: GhControl, workspace: Path, decide_block: str, gate_block: str, env: dict
) -> subprocess.CompletedProcess:
    """Compose the actual decide and gate run blocks like the build job does.

    Glue mirrors only the step-level wiring: a failed decide step fails the
    job, and the gate step runs when decide did not reuse (reused != 'true').
    """
    script = workspace / "decide_then_gate.sh"
    script.write_text(
        '#!/bin/bash\nset -u\n: > "$GITHUB_OUTPUT"\n'
        + decide_block
        + "\n"
        + "decide_rc=$?\n"
        + 'if [ "$decide_rc" -ne 0 ]; then\n'
        + '  echo "harness: decide step failed (exit $decide_rc);" '
        + '"the PyPI gate step would not run" >&2\n'
        + "  exit 1\n"
        + "fi\n"
        + 'reused="$(sed -n \'s/^reused=//p\' "$GITHUB_OUTPUT")"\n'
        + 'if [ "$reused" != "true" ]; then\n'
        + gate_block
        + "\nfi\n"
    )
    return subprocess.run(
        [BASH, str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


def extract_run_block(marker: str) -> str:
    """Extract a step's actual `run: |` body from publish.yml."""
    lines = PUBLISH_WORKFLOW.read_text().splitlines()
    try:
        step_at = next(i for i, line in enumerate(lines) if marker in line)
        run_at = next(i for i in range(step_at, len(lines)) if re.match(r"^\s*run: \|", lines[i]))
    except StopIteration as exc:
        pytest.fail(f"could not locate run block for {marker!r} in {PUBLISH_WORKFLOW}: {exc}")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip(" "))
    body = []
    for line in lines[run_at + 1 :]:
        if not line.strip():
            body.append("")
            continue
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        body.append(line[indent:])
    block = "\n".join(body).strip("\n") + "\n"
    assert block.strip(), f"empty run block extracted for {marker!r}"
    return block


def expand_runner_templates(block: str) -> str:
    for template, shell_form in RUNNER_TEMPLATE_EXPANSIONS.items():
        block = block.replace(template, shell_form)
    assert "${{" not in block, "unexpanded Actions template in run block"
    return block


@pytest.fixture(scope="session")
def decide_block():
    return extract_run_block(DECIDE_MARKER)


@pytest.fixture(scope="session")
def gate_block():
    return expand_runner_templates(extract_run_block(GATE_MARKER))


class TestExitCodeContract:
    """0/1/2 semantics are unchanged, via the real CLI."""

    def test_reuse_exit0_prints_run_and_artifact(self, gh):
        route_candidate(gh)
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == 0
        assert proc.stdout == f"42 {ARTIFACT}\n"

    def test_no_candidate_exit1(self, gh):
        gh.route(RUNS, envelope("workflow_runs", []))
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == 1
        assert "no prior validated build" in proc.stderr

    def test_expired_artifact_exit2(self, gh):
        gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
        gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
        gh.route(
            ENDPOINTS["artifacts"], envelope("artifacts", [{"name": ARTIFACT, "expired": True}])
        )
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == 2
        assert "has expired" in proc.stderr

    def test_absent_artifact_is_no_candidate_not_unusable(self, gh):
        """A missing artifact keeps its continue-as-no-candidate policy."""
        gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
        gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
        gh.route(ENDPOINTS["artifacts"], envelope("artifacts", []))
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == 1

    @pytest.mark.parametrize("custom_code", ["3", "7", "255"])
    def test_other_custom_unusable_codes_preserved(self, gh, custom_code):
        gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
        gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
        gh.route(
            ENDPOINTS["artifacts"], envelope("artifacts", [{"name": ARTIFACT, "expired": True}])
        )
        proc = run_finder(
            gh.env(),
            "--sha",
            SHA,
            "--tag",
            TAG,
            "--exclude-run",
            "999",
            "--unusable-artifact-code",
            custom_code,
        )
        assert proc.returncode == int(custom_code)

    @pytest.mark.parametrize("code", ["0", "1", "4", "256", "-1", "1000"])
    def test_reserved_or_out_of_range_unusable_code_rejected(self, gh, code):
        proc = run_finder(
            gh.env(),
            "--sha",
            SHA,
            "--tag",
            TAG,
            "--exclude-run",
            "999",
            "--unusable-artifact-code",
            code,
        )
        assert proc.returncode == 2  # parser.error usage exit, never remapped
        assert proc.stdout == ""
        assert "--unusable-artifact-code" in proc.stderr
        assert "1..255" in proc.stderr

    def test_usage_error_exit2_never_remapped(self, gh):
        proc = run_finder(gh.env(), "--tag", TAG)
        assert proc.returncode == 2
        assert "the following arguments are required" in proc.stderr


class TestUnknownStateExit4:
    """API/JSON/envelope/item/pagination/unexpected failures exit 4."""

    def test_gh_api_failure_is_actionable_exit4(self, gh):
        gh.route(RUNS, gh_fail())
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert proc.stdout == ""
        assert "::error::" in proc.stderr
        assert RUNS in proc.stderr
        assert "actions: read" in proc.stderr

    @pytest.mark.parametrize("endpoint", ["runs", "jobs", "artifacts"])
    def test_invalid_json_from_any_endpoint_is_unknown_state(self, gh, endpoint):
        route_candidate(gh)
        gh.route(ENDPOINTS[endpoint], {"code": 0, "raw": "<html>Bad Gateway</html>"})
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "invalid JSON" in proc.stderr
        assert ENDPOINTS[endpoint] in proc.stderr

    def test_non_object_envelope_is_unknown_state(self, gh):
        gh.route(RUNS, {"code": 0, "body": [{"id": 1}]})
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "not a JSON object" in proc.stderr

    @pytest.mark.parametrize(
        "endpoint,other_key",
        [
            ("runs", "jobs"),
            ("runs", "artifacts"),
            ("jobs", "workflow_runs"),
            ("artifacts", "workflow_runs"),
            ("artifacts", "jobs"),
        ],
    )
    def test_wrong_endpoint_items_key_is_unknown_state(self, gh, endpoint, other_key):
        """Each endpoint must return its own envelope items key."""
        route_candidate(gh)
        gh.route(ENDPOINTS[endpoint], envelope(other_key, []))
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert f"lacks a valid '{EXPECTED_KEY[endpoint]}' array" in proc.stderr

    def test_items_not_a_list_is_unknown_state(self, gh):
        gh.route(RUNS, envelope("workflow_runs", None, total=0))
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "lacks a valid 'workflow_runs' array" in proc.stderr

    @pytest.mark.parametrize(
        "endpoint,item",
        [("runs", "run-payload-as-string"), ("jobs", 7), ("artifacts", ARTIFACT)],
    )
    def test_malformed_item_is_unknown_state_not_absence(self, gh, endpoint, item):
        """A malformed item must not silently vanish into "no candidate"."""
        route_candidate(gh)
        gh.route(ENDPOINTS[endpoint], envelope(EXPECTED_KEY[endpoint], [item]))
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "not a JSON object" in proc.stderr

    def test_empty_envelope_without_items_key_is_unknown_state(self, gh):
        gh.route(RUNS, {"code": 0, "body": {"total_count": 0}})
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT

    @pytest.mark.parametrize(
        "second_page",
        [
            gh_fail(stderr="gh: HTTP 500 (server error on later page)"),
            {"code": 0, "raw": '{"workflow_runs": [truncated'},
            {"code": 0, "body": {"total_count": 100, "jobs": []}},
            {"code": 0, "body": {"total_count": 101, "workflow_runs": [{}]}},
        ],
    )
    def test_later_page_failure_is_unknown_state(self, gh, second_page):
        """A failure on page 2+ is unknown state, not a partial absence."""
        full_page = [trusted_run(1000 + i, num=i + 1) for i in range(fv.PER_PAGE)]
        gh.route(RUNS, envelope("workflow_runs", full_page, total=101), second_page)
        proc = run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "page=2" in proc.stderr or "page 2" in proc.stderr
        assert proc.stdout == ""

    def test_unlaunchable_gh_is_unexpected_failure_exit4(self, gh):
        # Isolated-bin PATH: the ENOENT exec of the broken stub gh has no
        # other PATH entry to fall through to (no real gh is reachable).
        proc = run_finder(
            gh.env(path=str(gh.isolated_bin(break_gh=True))),
            "--sha",
            SHA,
            "--tag",
            TAG,
            "--exclude-run",
            "999",
        )
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "unexpected operational failure" in proc.stderr

    def test_isolated_bin_gh_resolution_cannot_reach_host_gh(self, gh):
        """Regression: with an isolated PATH, gh resolves only to the stub —
        never to a real gh on the host (e.g. /usr/bin/gh on Linux)."""
        iso = gh.isolated_bin(break_gh=True)
        env = dict(os.environ)
        env["PATH"] = str(iso)
        resolved = subprocess.run(
            [BASH, "-c", "command -v gh"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert resolved.stdout.strip() == str(iso / "gh")
        proc = run_finder(gh.env(path=str(iso)), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "unexpected operational failure" in proc.stderr

    def test_absent_gh_binary_is_unexpected_failure_exit4(self, workspace):
        env = dict(os.environ)
        env["PATH"] = "/nonexistent-dir-for-tests"
        env["GH_REPO"] = "geofffranks/utec-py"
        proc = run_finder(env, "--sha", SHA, "--tag", TAG, "--exclude-run", "999")
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT
        assert "unexpected operational failure" in proc.stderr


def valid_run(**overrides) -> dict:
    """A fully-valid run with the real-API fields the decisions need."""
    run = trusted_run(42, num=7)
    run.update(overrides)
    return run


def drop(item: dict, field: str) -> dict:
    return {k: v for k, v in item.items() if k != field}


class TestItemFieldValidation:
    """Decision fields are validated before filtering/defaults — via the CLI.

    A missing/mistyped decision field is unknown state (exit 4) — in
    particular [{}] items can never be read as "no candidate" and an
    artifact without a real boolean `expired` can never be read as "not
    expired". Legitimate nonmatches (pull_request events, other workflows,
    null head branches, pending jobs, differently named artifacts) stay
    nonmatches.
    """

    MALFORMED_RUNS: ClassVar[list] = [
        ("empty-item", lambda r: {}),
        ("missing-id", lambda r: drop(r, "id")),
        ("string-id", lambda r: {**r, "id": "42"}),
        ("bool-id", lambda r: {**r, "id": True}),
        ("missing-path", lambda r: drop(r, "path")),
        ("numeric-path", lambda r: {**r, "path": 7}),
        ("missing-event", lambda r: drop(r, "event")),
        ("null-event", lambda r: {**r, "event": None}),
        ("numeric-event", lambda r: {**r, "event": 7}),
        ("missing-head-branch", lambda r: drop(r, "head_branch")),
        ("numeric-head-branch", lambda r: {**r, "head_branch": 5}),
        ("missing-run-number", lambda r: drop(r, "run_number")),
        ("string-run-number", lambda r: {**r, "run_number": "7"}),
        ("bool-run-number", lambda r: {**r, "run_number": True}),
        ("float-run-number", lambda r: {**r, "run_number": 7.5}),
    ]

    VALID_NONMATCH_RUNS: ClassVar[list] = [
        ("pull-request-event", {"event": "pull_request", "head_branch": "pr-1"}),
        ("other-workflow-path", {"path": ".github/workflows/other.yml"}),
        ("null-head-branch", {"head_branch": None}),
        ("untrusted-branch", {"head_branch": "feature"}),
    ]

    MALFORMED_JOBS: ClassVar[list] = [
        ("empty-item", lambda j: {}),
        ("missing-name", lambda j: {"conclusion": "success"}),
        ("numeric-name", lambda j: {"name": 7, "conclusion": "success"}),
        ("missing-conclusion", lambda j: {"name": "build"}),
        ("bool-conclusion", lambda j: {"name": "build", "conclusion": True}),
        ("numeric-conclusion", lambda j: {"name": "build", "conclusion": 0}),
    ]

    MALFORMED_ARTIFACTS: ClassVar[list] = [
        ("empty-item", lambda a: {}),
        ("missing-name", lambda a: {"expired": False}),
        ("numeric-name", lambda a: {"name": 42, "expired": False}),
        ("missing-expired", lambda a: {"name": ARTIFACT}),
        ("null-expired", lambda a: {"name": ARTIFACT, "expired": None}),
        ("string-expired-false", lambda a: {"name": ARTIFACT, "expired": "false"}),
        ("string-expired-true", lambda a: {"name": ARTIFACT, "expired": "true"}),
        ("int-expired", lambda a: {"name": ARTIFACT, "expired": 0}),
    ]

    @staticmethod
    def _run_item_case(gh: GhControl, endpoint: str, item: dict) -> subprocess.CompletedProcess:
        """Route a valid candidate but put `item` on `endpoint`."""
        route_candidate(gh)
        gh.route(ENDPOINTS[endpoint], envelope(EXPECTED_KEY[endpoint], [item]))
        return run_finder(gh.env(), "--sha", SHA, "--tag", TAG, "--exclude-run", "999")

    @pytest.mark.parametrize("label,mutate", MALFORMED_RUNS)
    def test_malformed_run_field_is_unknown_state(self, gh, label, mutate):
        proc = self._run_item_case(gh, "runs", mutate(valid_run()))
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT, proc.stderr
        assert proc.stdout == ""
        assert "'workflow_runs'[0]" in proc.stderr
        assert "unknown state" in proc.stderr

    @pytest.mark.parametrize("label,overrides", VALID_NONMATCH_RUNS)
    def test_valid_run_nonmatch_preserved(self, gh, label, overrides):
        proc = self._run_item_case(gh, "runs", valid_run(**overrides))
        assert proc.returncode == 1
        assert "no prior validated build" in proc.stderr

    @pytest.mark.parametrize("label,mutate", MALFORMED_JOBS)
    def test_malformed_job_field_is_unknown_state(self, gh, label, mutate):
        proc = self._run_item_case(gh, "jobs", mutate(build_job()))
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT, proc.stderr
        assert proc.stdout == ""
        assert "'jobs'[0]" in proc.stderr

    def test_pending_job_is_valid_nonmatch_not_error(self, gh):
        """Real API: conclusion is null while a job is pending/in-progress —
        valid, just never reusable."""
        proc = self._run_item_case(gh, "jobs", {"name": "build", "conclusion": None})
        assert proc.returncode == 1
        assert "no prior validated build" in proc.stderr

    @pytest.mark.parametrize("label,mutate", MALFORMED_ARTIFACTS)
    def test_malformed_artifact_field_is_unknown_state(self, gh, label, mutate):
        proc = self._run_item_case(gh, "artifacts", mutate(good_artifact()))
        assert proc.returncode == fv.UNKNOWN_STATE_EXIT, proc.stderr
        assert proc.stdout == ""  # in particular: never a reuse
        assert "'artifacts'[0]" in proc.stderr
        assert "unknown state" in proc.stderr

    def test_artifact_with_extra_fields_reuses(self, gh):
        """Only the decision fields are demanded; extra fields are fine."""
        proc = self._run_item_case(
            gh,
            "artifacts",
            {"name": ARTIFACT, "expired": False, "id": 123456, "size_in_bytes": 7},
        )
        assert proc.returncode == 0
        assert proc.stdout == f"42 {ARTIFACT}\n"

    def test_other_named_artifact_is_valid_nonmatch(self, gh):
        proc = self._run_item_case(gh, "artifacts", {"name": "other-dists", "expired": False})
        assert proc.returncode == 1
        assert "no prior validated build" in proc.stderr


class TestExecutableBoundary:
    """The exit-4 boundary catches Exception, never interrupts or usage."""

    def test_interrupts_propagate(self, monkeypatch):
        for exc_type in (KeyboardInterrupt, SystemExit):

            def raise_it(*args, _exc_type=exc_type, **kwargs):
                raise _exc_type()

            monkeypatch.setattr(fv, "_discover", raise_it)
            with pytest.raises(exc_type):
                fv.main(["--sha", SHA, "--tag", TAG, "--exclude-run", "999"])

    def test_unexpected_exception_becomes_exit4(self, monkeypatch, capsys):
        def explode(*args, **kwargs):
            raise ValueError("disk on fire")

        monkeypatch.setattr(fv, "_discover", explode)
        code = fv.main(["--sha", SHA, "--tag", TAG, "--exclude-run", "999"])
        assert code == fv.UNKNOWN_STATE_EXIT
        err = capsys.readouterr().err
        assert "::error::" in err
        assert "unexpected operational failure" in err
        assert "ValueError" in err

    def test_discovery_error_becomes_exit4(self, monkeypatch, capsys):
        def fail(*args, **kwargs):
            raise fv.DiscoveryError("gh api repos/x/actions/runs failed (exit 1): denied")

        monkeypatch.setattr(fv, "_discover", fail)
        code = fv.main(["--sha", SHA, "--tag", TAG, "--exclude-run", "999"])
        assert code == fv.UNKNOWN_STATE_EXIT
        assert "gh api repos/x/actions/runs failed" in capsys.readouterr().err


class TestWorkflowDecideStep:
    """The actual decide run block: outputs only for 0 and 1.

    Every harness run uses the isolated bin as the ONLY PATH entry (bash by
    absolute path; python/gh stubs plus symlinked awk/sed), so the blocks
    can never reach a host tool.
    """

    def test_reuse_writes_reuse_outputs(self, gh, workspace, decide_block):
        route_candidate(gh)
        env = iso_env(gh, workspace)
        proc = run_decide_step(gh, workspace, decide_block, env)
        assert proc.returncode == 0
        assert outputs_of(env) == {
            "reused": "true",
            "reused_run_id": "42",
            "artifact_name": ARTIFACT,
            "built_by_run": "42",
        }

    def test_no_candidate_writes_reused_false(self, gh, workspace, decide_block):
        gh.route(RUNS, envelope("workflow_runs", []))
        env = iso_env(gh, workspace)
        proc = run_decide_step(gh, workspace, decide_block, env)
        assert proc.returncode == 0
        assert outputs_of(env) == {
            "reused": "false",
            "built_by_run": "999",
        }

    def test_expired_fails_before_outputs(self, gh, workspace, decide_block):
        gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
        gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
        gh.route(
            ENDPOINTS["artifacts"], envelope("artifacts", [{"name": ARTIFACT, "expired": True}])
        )
        env = iso_env(gh, workspace)
        proc = run_decide_step(gh, workspace, decide_block, env)
        assert proc.returncode != 0
        assert Path(env["GITHUB_OUTPUT"]).read_text() == ""
        assert "has expired" in proc.stderr  # finder guidance reached the log

    def test_unknown_state_fails_before_outputs(self, gh, workspace, decide_block):
        gh.route(RUNS, gh_fail())
        env = iso_env(gh, workspace)
        proc = run_decide_step(gh, workspace, decide_block, env)
        assert proc.returncode != 0
        assert Path(env["GITHUB_OUTPUT"]).read_text() == ""
        # The finder's own actionable guidance goes to stderr; the step's
        # ::error:: annotation goes to stdout (as on Actions runners).
        assert "gh api" in proc.stderr
        assert "discovery failed with exit 4" in proc.stdout

    def test_unexpected_failure_fails_before_outputs(self, gh, workspace, decide_block):
        env = iso_env(gh, workspace, break_gh=True)
        proc = run_decide_step(gh, workspace, decide_block, env)
        assert proc.returncode != 0
        assert Path(env["GITHUB_OUTPUT"]).read_text() == ""
        assert "discovery failed with exit 4" in proc.stdout


class TestWorkflowGateComposition:
    """Only "no candidate" reaches the independent PyPI rebuild gate."""

    def test_unknown_state_blocks_gate_even_when_pypi_empty(
        self, gh, workspace, decide_block, gate_block
    ):
        gh.route(RUNS, gh_fail())
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="1")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode != 0
        assert Path(env["GITHUB_OUTPUT"]).read_text() == ""
        assert not Path(env["PREFLIGHT_CALLS"]).exists()

    def test_unexpected_failure_blocks_gate_even_when_pypi_empty(
        self, gh, workspace, decide_block, gate_block
    ):
        env = iso_env(gh, workspace, break_gh=True, FAKE_PREFLIGHT_CODE="1")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode != 0
        assert not Path(env["PREFLIGHT_CALLS"]).exists()

    def test_expired_artifact_blocks_gate(self, gh, workspace, decide_block, gate_block):
        gh.route(RUNS, envelope("workflow_runs", [trusted_run(42)]))
        gh.route(ENDPOINTS["jobs"], envelope("jobs", [build_job()]))
        gh.route(
            ENDPOINTS["artifacts"], envelope("artifacts", [{"name": ARTIFACT, "expired": True}])
        )
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="1")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode != 0
        assert not Path(env["PREFLIGHT_CALLS"]).exists()

    def test_no_candidate_empty_pypi_reaches_gate_and_builds(
        self, gh, workspace, decide_block, gate_block
    ):
        gh.route(RUNS, envelope("workflow_runs", []))
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="1")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode == 0
        assert outputs_of(env) == {"reused": "false", "built_by_run": "999"}
        calls = Path(env["PREFLIGHT_CALLS"]).read_text()
        assert calls.count("\n") == 1
        assert "--check-files-exist" in calls

    def test_no_candidate_with_pypi_files_rebuild_denied(
        self, gh, workspace, decide_block, gate_block
    ):
        gh.route(RUNS, envelope("workflow_runs", []))
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="0")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode != 0
        assert "refusing to rebuild" in proc.stdout  # gate ::error:: goes to stdout

    def test_no_candidate_unknown_pypi_state_fails_closed(
        self, gh, workspace, decide_block, gate_block
    ):
        gh.route(RUNS, envelope("workflow_runs", []))
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="4")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode != 0
        assert "could not determine PyPI state" in proc.stdout

    def test_reuse_skips_gate_entirely(self, gh, workspace, decide_block, gate_block):
        route_candidate(gh)
        env = iso_env(gh, workspace, FAKE_PREFLIGHT_CODE="0")
        proc = run_decide_then_gate(gh, workspace, decide_block, gate_block, env)
        assert proc.returncode == 0
        assert outputs_of(env)["reused"] == "true"
        assert not Path(env["PREFLIGHT_CALLS"]).exists()
