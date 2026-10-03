"""Integrated simulated tests for the common validated publishing path.

Covers the second-repair-round blockers:
- preflight strict distribution filtering (SHA256SUMS never staged/readback)
- first human build (no prior validated artifact -> build path)
- failed validation blocking publication (untrusted runs never reused)
- retained retry reuse (prior successful trusted run's artifact reused)
- source/version mismatch and conflicting artifacts refuse publication
"""

import hashlib
import json

import pypi_preflight as pp
import verify_assets as va
import verify_validated_build as vvb
from find_validated_build import (
    ARTIFACT_NAME,
    TRUSTED_WORKFLOW_PATH,
)

TAG, VERSION, SHA = "v1.0.0", "1.0.0", "a" * 40


def make_valid_dist(tmp_path, *, tag=TAG, version=VERSION, sha=SHA, corrupt_manifest=False):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    wheel = dist / f"utec_client-{version}-py3-none-any.whl"
    sdist = dist / f"utec_client-{version}.tar.gz"
    wheel.write_bytes(b"wheel-bytes-" + version.encode())
    sdist.write_bytes(b"sdist-bytes-" + version.encode())
    (dist / "PROVENANCE.json").write_text(
        json.dumps({"tag": tag, "version": version, "source_sha": sha, "built_by_run": 111})
    )
    lines = []
    for f in (wheel, sdist):
        content = b"tampered" if corrupt_manifest else f.read_bytes()
        lines.append(f"{hashlib.sha256(content).hexdigest()}  {f.name}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return dist


class TestManifestExcluded:
    """Blocker 1: SHA256SUMS in dist/ is never a distribution."""

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
        assert "SHA256SUMS" not in names
        assert "PROVENANCE.json" not in names

    def test_readback_ignores_manifest(self, tmp_path, capsys):
        dist = make_valid_dist(tmp_path)
        upstream = {
            name: {"digests": {"sha256": d}} for name, d in pp.local_dists(str(dist)).items()
        }
        upstream["PROVENANCE.json"] = {"digests": {"sha256": "0" * 64}}
        monkey_upstream(pp, upstream)
        assert pp.cmd_verify_published(ns(str(dist))) == 0


class TestFindValidatedBuild:
    """Only successful, trusted, exact-source runs with the artifact qualify."""

    def _runs(self, monkeypatch, runs, artifacts=None):
        import find_validated_build as fv

        def fake_gh(*args):
            assert args[0] == "api"
            if "actions/runs" in args[1] and "artifacts" not in args[1]:
                return "".join(json.dumps(r) + "\n" for r in runs)
            assert args[1].endswith("/artifacts")
            return "".join(json.dumps(a) + "\n" for a in artifacts or [])

        monkeypatch.setattr(fv, "gh", fake_gh)
        monkeypatch.setattr(fv, "repo_slug", lambda: "geofffranks/utec-py")
        return fv

    def test_first_human_build_has_no_candidate(self, monkeypatch, capsys):
        fv = self._runs(monkeypatch, [])
        assert fv.main_args("--sha", SHA, "--exclude-run", "999") == 1

    def test_failed_validation_run_never_reused(self, monkeypatch):
        runs = [
            {
                "id": 1,
                "head_sha": SHA,
                "conclusion": "failure",
                "path": TRUSTED_WORKFLOW_PATH,
                "event": "release",
            }
        ]
        fv = self._runs(monkeypatch, runs)
        assert fv.successful_runs(SHA) == []

    def test_matching_run_selected_and_current_excluded(self, monkeypatch):
        trusted = lambda id, sha=SHA, event="release": {  # noqa: E731
            "id": id,
            "head_sha": sha,
            "conclusion": "success",
            "path": TRUSTED_WORKFLOW_PATH,
            "event": event,
            "run_number": id,
        }
        runs = [
            trusted(1),
            trusted(5),  # current run, excluded
            trusted(7),
            trusted(8, sha="b" * 40),  # different commit
            trusted(9, event="pull_request"),  # untrusted event
            {
                "id": 10,
                "head_sha": SHA,
                "conclusion": "success",
                "path": ".github/workflows/other.yml",
                "event": "workflow_dispatch",
            },  # wrong workflow
        ]
        fv = self._runs(monkeypatch, runs, artifacts=[{"name": ARTIFACT_NAME, "expired": False}])
        candidates = fv.successful_runs(SHA)
        assert {r["id"] for r in candidates} == {1, 5, 7}  # exclusion happens at main()
        assert fv.main_args("--sha", SHA, "--exclude-run", "5") == 0

    def test_run_without_artifact_not_reused(self, monkeypatch):
        runs = [
            {
                "id": 1,
                "head_sha": SHA,
                "conclusion": "success",
                "path": TRUSTED_WORKFLOW_PATH,
                "event": "workflow_dispatch",
            }
        ]
        fv = self._runs(monkeypatch, runs, artifacts=[])
        assert fv.main_args("--sha", SHA, "--exclude-run", "5") == 1

    def test_expired_artifact_not_reused(self, monkeypatch):
        runs = [
            {
                "id": 1,
                "head_sha": SHA,
                "conclusion": "success",
                "path": TRUSTED_WORKFLOW_PATH,
                "event": "schedule",
            }
        ]
        fv = self._runs(monkeypatch, runs, artifacts=[{"name": ARTIFACT_NAME, "expired": True}])
        assert fv.main_args("--sha", SHA, "--exclude-run", "5") == 1


class TestVerifyValidatedBuild:
    def test_valid_artifact_passes(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", SHA]) == 0

    def test_version_mismatch_refused(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", "9.9.9", "--sha", SHA]) == 1

    def test_source_sha_mismatch_refused(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        assert (
            vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", "b" * 40])
            == 1
        )

    def test_wrong_version_distributions_refused(self, tmp_path):
        dist = make_valid_dist(tmp_path, version="0.9.0")
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", SHA]) == 1

    def test_conflicting_manifest_refused(self, tmp_path):
        dist = make_valid_dist(tmp_path, corrupt_manifest=True)
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", SHA]) == 1

    def test_missing_provenance_refused(self, tmp_path):
        dist = make_valid_dist(tmp_path)
        (dist / "PROVENANCE.json").unlink()
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", SHA]) == 1

    def test_manifest_alone_is_not_sufficient(self, tmp_path):
        # Self-consistent manifest but provenance binding to another commit:
        # the reviewer's attack case must fail.
        dist = make_valid_dist(tmp_path, sha="b" * 40)
        assert vvb.main(["--dir", str(dist), "--tag", TAG, "--version", VERSION, "--sha", SHA]) == 1

    def test_verify_assets_still_works(self, tmp_path):
        dist = make_valid_dist(tmp_path)
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


def ns(dist, staging=None):
    import argparse as _ap

    return _ap.Namespace(
        dist_dir=dist, project="utec-client", staging_dir=staging, verify_published=False
    )


def monkey_upstream(pp, by_name):
    pp.fetch_pypi = lambda project: dict(by_name)
