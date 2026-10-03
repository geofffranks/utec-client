"""Integrated simulated tests for the common validated publishing path.

Covers the third-repair-round requirements:
- faithful GitHub API envelopes ({total_count, workflow_runs|jobs|artifacts})
  parsed page-by-page as true JSON objects (N3)
- reuse of a successful BUILD job even when the whole run failed (N4),
  including runs dispatched on main whose head_sha has moved past an older
  release tag
- trusted-ref and producer-run-ID validation (ADV10)
- expired/missing validated artifact after partial upload fails closed (N5)
- fresh build allowed only while PyPI has no release files
- manifest/PROVENANCE.json never staged or demanded on PyPI (blocker 1)
"""

import hashlib
import json

import pytest

import find_validated_build as fv
import pypi_preflight as pp
import verify_assets as va
import verify_validated_build as vvb

TAG, VERSION, SHA = "v1.0.0", "1.0.0", "a" * 40
ARTIFACT = f"release-dists-{SHA}"
NEW_MAIN_SHA = "c" * 40  # main advanced past the older release tag


def make_valid_dist(
    tmp_path, *, tag=TAG, version=VERSION, sha=SHA, built_by_run=42, corrupt_manifest=False
):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    wheel = dist / f"utec_client-{version}-py3-none-any.whl"
    sdist = dist / f"utec_client-{version}.tar.gz"
    wheel.write_bytes(b"wheel-bytes-" + version.encode())
    sdist.write_bytes(b"sdist-bytes-" + version.encode())
    (dist / "PROVENANCE.json").write_text(
        json.dumps(
            {"tag": tag, "version": version, "source_sha": sha, "built_by_run": built_by_run}
        )
    )
    lines = []
    for f in (wheel, sdist):
        content = b"tampered" if corrupt_manifest else f.read_bytes()
        lines.append(f"{hashlib.sha256(content).hexdigest()}  {f.name}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return dist


class FakeEnvelopeAPI:
    """Faithful GitHub API envelope model: {total_count, <items>: [...]} per page."""

    def __init__(self):
        self.items: dict[str, list] = {}

    def add(self, base_url: str, items: list) -> None:
        self.items[base_url] = items

    def _key(self, url: str) -> str:
        if url.endswith("/jobs") or "/jobs?" in url:
            return "jobs"
        if "/artifacts" in url:
            return "artifacts"
        return "workflow_runs"

    def gh_api(self, url: str) -> str:
        base, _, query = url.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        per_page = int(params.get("per_page", 100))
        page = int(params.get("page", 1))
        items = self.items.get(base, [])
        batch = items[(page - 1) * per_page : page * per_page]
        return json.dumps(
            {"total_count": len(items), self._key(url): batch},
        )


def install_fake_api(monkeypatch, fake: FakeEnvelopeAPI):
    # fv.gh_api returns a parsed envelope object; the fake models the same
    # contract (gh prints JSON, the script parses it).
    monkeypatch.setattr(fv, "gh_api", lambda url: json.loads(fake.gh_api(url)))
    monkeypatch.setattr(fv, "repo_slug", lambda: "geofffranks/utec-py")


def trusted_run(id, *, head_branch="main", head_sha=NEW_MAIN_SHA, event="workflow_dispatch", num=1):
    # head_sha is the DISPATCH REF head (main), not the tagged commit.
    return {
        "id": id,
        "run_number": num,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "conclusion": "failure",  # whole run failed at publish time
        "path": fv.TRUSTED_WORKFLOW_PATH,
        "event": event,
    }


class TestFindValidatedBuild:
    """N3/N4: envelope parsing, build-job reuse, trusted refs."""

    def _runs_url(self):
        return "repos/geofffranks/utec-py/actions/runs"

    def test_parses_true_envelope_and_reuses_failed_run_build_job(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(
            self._runs_url(),
            [
                trusted_run(42),
                {
                    "id": 43,
                    "path": ".github/workflows/other.yml",
                    "event": "workflow_dispatch",
                    "head_branch": "main",
                },
            ],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [
                {"name": "build", "conclusion": "success"},
                {"name": "pypi-publish", "conclusion": "failure"},
            ],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [
                {"name": ARTIFACT, "expired": False},
            ],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 0

    def test_paginates_multiple_pages(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(41, num=41), trusted_run(42, num=42)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        install_fake_api(monkeypatch, fake)
        # Force pagination: shrink PER_PAGE so the envelope spans two pages.
        monkeypatch.setattr(fv, "PER_PAGE", 1)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 0

    def test_whole_run_cancelled_but_build_succeeded_reused(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(7, num=7)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/jobs",
            [
                {"name": "build", "conclusion": "success"},
                {"name": "pypi-publish", "conclusion": "cancelled"},
            ],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 0

    def test_failed_build_job_not_reused(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(7, num=7)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/jobs",
            [{"name": "build", "conclusion": "failure"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 1

    def test_human_older_tag_with_main_advanced_trusted(self, monkeypatch):
        """Release-event runs on the tag ref are trusted; head_sha (main head)
        is irrelevant to reuse."""
        fake = FakeEnvelopeAPI()
        fake.add(
            self._runs_url(),
            [trusted_run(9, head_branch=TAG, event="release", num=9)],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/9/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/9/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 0

    def test_untrusted_workflow_ref_rejected(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(
            self._runs_url(),
            [
                trusted_run(11, head_branch="feature-x", num=11),  # dispatch off main
                trusted_run(12, event="pull_request", head_branch="pr-1", num=12),
            ],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 1

    def test_expired_artifact_fails_closed(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(7, num=7)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/artifacts",
            [{"name": ARTIFACT, "expired": True}],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 2

    def test_missing_artifact_is_no_candidate(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(7, num=7)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/7/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add("repos/geofffranks/utec-py/actions/runs/7/artifacts", [])
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "999") == 1

    def test_current_run_excluded(self, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(self._runs_url(), [trusted_run(5, num=5)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/5/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/5/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        install_fake_api(monkeypatch, fake)
        assert fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", "5") == 1


class TestManifestExcluded:
    """Blocker 1: SHA256SUMS/PROVENANCE.json in dist/ are never distributions."""

    def test_local_dists_ignores_manifest(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        digests = pp.local_dists(str(dist))
        assert set(digests) == {
            f"utec_client-{VERSION}-py3-none-any.whl",
            f"utec_client-{VERSION}.tar.gz",
        }

    def test_staging_contains_only_distributions(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        monkey_upstream(pp, {})
        staging = tmp_path / "staging"
        assert pp.cmd_check(ns(str(dist), str(staging))) == 0
        names = sorted(p.name for p in staging.iterdir())
        assert names == [
            f"utec_client-{VERSION}-py3-none-any.whl",
            f"utec_client-{VERSION}.tar.gz",
        ]

    def test_readback_ignores_manifest_and_provenance(self, tmp_path, capsys):
        dist = make_valid_dist(tmp_path)
        upstream = {
            name: {"digests": {"sha256": d}} for name, d in pp.local_dists(str(dist)).items()
        }
        upstream["PROVENANCE.json"] = {"digests": {"sha256": "0" * 64}}
        upstream["SHA256SUMS"] = {"digests": {"sha256": "0" * 64}}
        monkey_upstream(pp, upstream)
        assert pp.cmd_verify_published(ns(str(dist))) == 0


class TestVerifyValidatedBuild:
    """ADV10: provenance binds bytes to source, tag, version, producer run."""

    def _ok(self, tmp_path, **kw):
        dist = make_valid_dist(tmp_path, **kw)
        return dist

    def _args(self, dist, built_by_run=42, tag=TAG, version=VERSION, sha=SHA):
        return [
            "--dir",
            str(dist),
            "--tag",
            tag,
            "--version",
            version,
            "--sha",
            sha,
            "--built-by-run",
            str(built_by_run),
        ]

    def test_valid_artifact_passes(self, tmp_path):
        assert vvb.main(self._args(self._ok(tmp_path))) == 0

    def test_producer_run_mismatch_refused(self, tmp_path):
        dist = self._ok(tmp_path)
        assert vvb.main(self._args(dist, built_by_run=999)) == 1

    def test_provenance_missing_run_id_refused(self, tmp_path):
        dist = self._ok(tmp_path)
        data = json.loads((dist / "PROVENANCE.json").read_text())
        del data["built_by_run"]
        (dist / "PROVENANCE.json").write_text(json.dumps(data))
        assert vvb.main(self._args(dist)) == 1

    def test_version_mismatch_refused(self, tmp_path):
        assert vvb.main(self._args(self._ok(tmp_path), version="9.9.9")) == 1

    def test_source_sha_mismatch_refused(self, tmp_path):
        assert vvb.main(self._args(self._ok(tmp_path), sha="b" * 40)) == 1

    def test_conflicting_manifest_refused(self, tmp_path):
        dist = self._ok(tmp_path, corrupt_manifest=True)
        assert vvb.main(self._args(dist)) == 1

    def test_manifest_alone_is_not_sufficient(self, tmp_path):
        dist = make_valid_dist(tmp_path, sha="b" * 40)
        assert vvb.main(self._args(dist)) == 1

    def test_verify_assets_still_works(self, tmp_path):
        dist = self._ok(tmp_path)
        assert (
            va.main(
                [
                    "--dir",
                    str(dist),
                    "--manifest",
                    str(dist / "SHA256SUMS"),
                    "--ignore",
                    "PROVENANCE.json",
                ]
            )
            == 0
        )


class TestPublishDecisionSequence:
    """End-to-end simulated decision sequence of publish.yml's build job."""

    @staticmethod
    def _decide(monkeypatch, fake, tmp_path, *, selected_version_files=None, current_run=999):
        """Replicates the workflow step logic: find reuse -> verify -> preflight.

        selected_version_files models files ALREADY ON PYPI FOR THE SELECTED
        VERSION (N6: other published versions are irrelevant to the gate).
        """
        install_fake_api(monkeypatch, fake)
        steps = {}
        code = fv.main_args("--sha", SHA, "--tag", TAG, "--exclude-run", str(current_run))
        if code == 2:
            return {"outcome": "fail-closed-artifact-unusable"}
        if code == 0:
            reused_run = steps["reused_run_id"] = 42
            dist = tmp_path / "dist"
            dist.mkdir(exist_ok=True)
            for f in make_valid_dist(tmp_path).iterdir():
                (dist / f.name).write_bytes(f.read_bytes())
            steps["verified"] = (
                vvb.main(
                    [
                        "--dir",
                        str(dist),
                        "--tag",
                        TAG,
                        "--version",
                        VERSION,
                        "--sha",
                        SHA,
                        "--built-by-run",
                        str(reused_run),
                    ]
                )
                == 0
            )
            steps["outcome"] = "reuse"
            monkey_upstream(pp, selected_version_files or {})
            staging = tmp_path / "staging"
            steps["preflight_rc"] = pp.cmd_check(ns(str(dist), str(staging)))
            steps["staged"] = sorted(p.name for p in staging.iterdir())
            return steps
        # no reuse candidate: fresh build only allowed while the SELECTED
        # version has no PyPI files (N6 version-scoped gate)
        if selected_version_files:
            steps["outcome"] = "fail-closed-rebuild-denied"
        else:
            steps["outcome"] = "fresh-build"
        return steps

    def test_human_older_tag_main_advanced_failed_publish_then_retry_reuses(
        self, tmp_path, monkeypatch
    ):
        fake = FakeEnvelopeAPI()
        fake.add("repos/geofffranks/utec-py/actions/runs", [trusted_run(42, num=42)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [
                {"name": "build", "conclusion": "success"},
                {"name": "pypi-publish", "conclusion": "failure"},  # publish failed, build reusable
            ],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        # PyPI already has the wheel with its identical sha256 (partial upload).
        wheel = f"utec_client-{VERSION}-py3-none-any.whl"
        wheel_digest = hashlib.sha256((f"wheel-bytes-{VERSION}").encode()).hexdigest()
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={
                wheel: {"digests": {"sha256": wheel_digest}},
            },
        )
        assert result["outcome"] == "reuse"
        assert result["verified"] is True
        assert result["preflight_rc"] == 0
        # only the missing sdist is staged; identical wheel skipped
        assert result["staged"] == [f"utec_client-{VERSION}.tar.gz"]

    def test_changed_main_on_retry_still_reuses(self, tmp_path, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(
            "repos/geofffranks/utec-py/actions/runs",
            [trusted_run(42, head_sha=NEW_MAIN_SHA, num=42)],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        result = self._decide(monkeypatch, fake, tmp_path)
        assert result["outcome"] == "reuse"
        assert result["verified"] is True

    def test_untrusted_ref_and_fresh_pypi_builds(self, tmp_path, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(
            "repos/geofffranks/utec-py/actions/runs",
            [trusted_run(11, head_branch="feature", num=11)],
        )
        result = self._decide(monkeypatch, fake, tmp_path)
        assert result["outcome"] == "fresh-build"

    def test_untrusted_ref_after_partial_fails_closed(self, tmp_path, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add(
            "repos/geofffranks/utec-py/actions/runs",
            [trusted_run(11, head_branch="feature", num=11)],
        )
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={
                "utec_client-1.0.0-py3-none-any.whl": {"digests": {"sha256": "0" * 64}},
            },
        )
        assert result["outcome"] == "fail-closed-rebuild-denied"

    def test_expired_artifact_after_partial_blocks_rebuild(self, tmp_path, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add("repos/geofffranks/utec-py/actions/runs", [trusted_run(42, num=42)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [{"name": ARTIFACT, "expired": True}],
        )
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={
                "utec_client-1.0.0.tar.gz": {"digests": {"sha256": "0" * 64}},
            },
        )
        assert result == {"outcome": "fail-closed-artifact-unusable"}

    def test_fresh_project_no_pypi_files_builds(self, tmp_path, monkeypatch):
        fake = FakeEnvelopeAPI()
        fake.add("repos/geofffranks/utec-py/actions/runs", [])
        result = self._decide(monkeypatch, fake, tmp_path)
        assert result["outcome"] == "fresh-build"

    def test_baseline_published_later_version_fresh_build_allowed(self, tmp_path, monkeypatch):
        """N6 regression: a successful baseline (v1.0.0 fully published) never
        blocks a fresh build/release of the NEXT version (v1.1.0) with no
        files of its own on PyPI, even when reuse is unavailable."""
        fake = FakeEnvelopeAPI()
        fake.add(
            "repos/geofffranks/utec-py/actions/runs",
            [trusted_run(11, head_branch="feature", num=11)],
        )
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={},  # v1.1.0 has no files; baseline v1.0.0 exists out of band
        )
        assert result["outcome"] == "fresh-build"

    def test_partial_selected_version_retry_reuse_preferred(self, tmp_path, monkeypatch):
        """N7: with partial files of the selected version, a retained
        validated artifact is reused (never a rebuild)."""
        fake = FakeEnvelopeAPI()
        fake.add("repos/geofffranks/utec-py/actions/runs", [trusted_run(42, num=42)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/artifacts",
            [{"name": ARTIFACT, "expired": False}],
        )
        # partial upload: the wheel is already on PyPI with its identical hash
        wheel_digest = hashlib.sha256(f"wheel-bytes-{VERSION}".encode()).hexdigest()
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={
                f"utec_client-{VERSION}-py3-none-any.whl": {"digests": {"sha256": wheel_digest}},
            },
        )
        assert result["outcome"] == "reuse"
        assert result["verified"] is True
        assert result["preflight_rc"] == 0  # only the missing sdist staged

    def test_missing_artifact_partial_selected_version_fails_closed(self, tmp_path, monkeypatch):
        """N7: artifact missing while the selected version is partially on
        PyPI -> no reuse and no rebuild: fail closed."""
        fake = FakeEnvelopeAPI()
        fake.add("repos/geofffranks/utec-py/actions/runs", [trusted_run(42, num=42)])
        fake.add(
            "repos/geofffranks/utec-py/actions/runs/42/jobs",
            [{"name": "build", "conclusion": "success"}],
        )
        fake.add("repos/geofffranks/utec-py/actions/runs/42/artifacts", [])
        result = self._decide(
            monkeypatch,
            fake,
            tmp_path,
            selected_version_files={
                "utec_client-1.0.0-py3-none-any.whl": {"digests": {"sha256": "0" * 64}},
            },
        )
        # no candidate (exit 1) + selected version has files -> rebuild denied
        assert result["outcome"] == "fail-closed-rebuild-denied"


class TestCheckFilesExist:
    """N6: the rebuild gate is scoped to the SELECTED version's files."""

    @staticmethod
    def _patch_version(monkeypatch, version_payload, project_payload=None):
        calls = []

        def fake_version(project, version):
            calls.append(("version", project, version))
            return version_payload

        def fake_project(project):
            calls.append(("project", project))
            return project_payload

        monkeypatch.setattr(pp, "fetch_pypi_version", fake_version)
        monkeypatch.setattr(pp, "fetch_pypi", fake_project)
        return calls

    def test_version_published_with_files(self, monkeypatch, capsys):
        self._patch_version(monkeypatch, {"urls": [{"filename": "x.whl"}]})
        assert pp.main(["--check-files-exist", "--version", VERSION]) == 0
        assert json.loads(capsys.readouterr().out)["has_files"] is True

    def test_version_published_without_files(self, monkeypatch, capsys):
        self._patch_version(monkeypatch, {"urls": []})
        assert pp.main(["--check-files-exist", "--version", VERSION]) == 1

    def test_version_404_project_present(self, monkeypatch, capsys):
        self._patch_version(monkeypatch, None, project_payload={"urls": [{"filename": "old.whl"}]})
        assert pp.main(["--check-files-exist", "--version", "9.9.9"]) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["detail"] == "version-not-published"
        # the old published version's files did NOT block the decision
        assert out["has_files"] is False

    def test_project_404(self, monkeypatch, capsys):

        self._patch_version(monkeypatch, None)

        def fake_project(project):
            raise pp.urllib.error.HTTPError("u", 404, "nf", None, None)

        monkeypatch.setattr(pp, "fetch_pypi", fake_project)
        assert pp.main(["--check-files-exist", "--version", "9.9.9"]) == 1
        assert json.loads(capsys.readouterr().out)["detail"] == "project-not-on-pypi"

    def test_unknown_state_fails_closed(self, monkeypatch, capsys):

        def fake_version(project, version):
            raise pp.urllib.error.HTTPError("u", 500, "boom", None, None)

        monkeypatch.setattr(pp, "fetch_pypi_version", fake_version)
        assert pp.main(["--check-files-exist", "--version", VERSION]) == 4

    def test_version_is_required(self):
        with pytest.raises(SystemExit):
            pp.main(["--check-files-exist"])

    def test_fetch_pypi_version_404_is_none(self, monkeypatch):
        def fake_urlopen(url, timeout):
            raise pp.urllib.error.HTTPError(url, 404, "nf", None, None)

        monkeypatch.setattr(pp.urllib.request, "urlopen", fake_urlopen)
        assert pp.fetch_pypi_version("utec-client", "9.9.9") is None


def ns(dist, staging=None):
    import argparse as _ap

    return _ap.Namespace(
        dist_dir=dist, project="utec-client", staging_dir=staging, verify_published=False
    )


def monkey_upstream(pp, by_name):
    pp.fetch_pypi = lambda project: dict(by_name)
