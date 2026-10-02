"""Regression tests for the workflow plumbing: gh/git invocations, release
asset verification, and PyPI preflight/readback classification.

The pure decision logic is covered in test_release_logic.py; these tests pin
the actual plumbing contracts (command shapes and data flow) that the
workflows depend on, using mocked gh/git/urllib.
"""

import hashlib
import json
import subprocess

import pytest

import prepare_release as pr
import pypi_preflight as pp
from release_logic import (
    DEFAULT_FORK_BASE_COMMIT,
    FORK_RELEASE_MARKER,
    is_eligible_fork_release,
    previous_fork_tag,
)


def write_dist(tmp_path, files: dict[str, bytes]) -> str:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for name, content in files.items():
        (dist / name).write_bytes(content)
    return str(dist)


class TestEligibilityBoundary:
    """ADV4: marker alone is not enough; the ancestry check excludes inherited tags."""

    def test_marked_post_fork_release_eligible(self):
        assert is_eligible_fork_release(has_marker=True, base_is_ancestor=True, same_commit=False)

    def test_inherited_tag_with_forged_marker_refused(self):
        assert not is_eligible_fork_release(
            has_marker=True, base_is_ancestor=False, same_commit=False
        )

    def test_tag_at_fork_base_refused(self):
        assert not is_eligible_fork_release(
            has_marker=True, base_is_ancestor=True, same_commit=True
        )

    def test_unmarked_post_fork_release_refused(self):
        assert not is_eligible_fork_release(
            has_marker=False, base_is_ancestor=True, same_commit=False
        )


class TestPreviousForkTag:
    def test_highest_lower_tag(self):
        releases = [{"tag": "v1.0.0"}, {"tag": "v0.9.0"}, {"tag": "v1.2.0"}]
        assert previous_fork_tag(releases, "v1.1.0") == "v1.0.0"

    def test_first_release_has_no_previous(self):
        assert previous_fork_tag([{"tag": "v1.0.0"}], "v1.0.0") is None


class TestListForkReleases:
    """C1: release listing must use the paginated REST API (gh api), because
    `gh release list --json` does not return release bodies."""

    BASE = DEFAULT_FORK_BASE_COMMIT

    def _mock_gh(self, monkeypatch, releases_json):
        calls = []

        def fake_gh(*args):
            calls.append(args)
            return releases_json

        monkeypatch.setattr(pr, "gh", fake_gh)
        return calls

    def _mock_git(self, monkeypatch, shas):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "rev-list":
                return shas[args[2]] + "\n"
            if args[0] == "remote":
                return "https://github.com/geofffranks/utec-py.git\n"
            if args[0] == "merge-base":  # not used; merge-base runs via subprocess
                return ""
            raise AssertionError(f"unexpected git call {args}")

        monkeypatch.setattr(pr, "git", fake_git)
        monkeypatch.setattr(pr, "repo_slug", lambda: "geofffranks/utec-py")
        return calls

    def _mock_ancestry(self, monkeypatch, base_is_ancestor, same_commit=False):
        def fake_run(cmd, capture_output):
            assert cmd[:2] == ["git", "merge-base"]
            ok = base_is_ancestor
            return subprocess.CompletedProcess(cmd, 0 if ok and not same_commit else 1, b"", b"")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)

    def test_uses_paginated_rest_api(self, monkeypatch):
        body = json.dumps(
            {
                "tag_name": "v1.0.0",
                "draft": False,
                "prerelease": False,
                "body": FORK_RELEASE_MARKER,
            }
        )
        calls = self._mock_gh(monkeypatch, body)
        self._mock_git(monkeypatch, {"v1.0.0": "a" * 40})
        self._mock_ancestry(monkeypatch, base_is_ancestor=True)
        fork = pr.list_fork_releases()
        assert calls[0][0] == "api"
        assert calls[0][1] == "repos/geofffranks/utec-py/releases"
        assert "--paginate" in calls[0]
        assert "--slurp" not in calls[0]  # gh forbids --slurp with --jq
        assert fork == [{"tag": "v1.0.0", "sha": "a" * 40}]

    def test_inherited_release_with_forged_marker_excluded(self, monkeypatch):
        body = "\n".join(
            json.dumps(r)
            for r in [
                {
                    "tag_name": "v0.4.1",
                    "draft": False,
                    "prerelease": False,
                    "body": FORK_RELEASE_MARKER,  # forged marker on inherited tag
                },
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": FORK_RELEASE_MARKER,
                },
            ]
        )
        self._mock_gh(monkeypatch, body)
        self._mock_git(monkeypatch, {"v0.4.1": "b" * 40, "v1.0.0": "a" * 40})

        def fake_run(cmd, capture_output):
            ok = cmd[4] == "a" * 40  # cmd: git merge-base --is-ancestor <base> <sha>
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"", b"")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        fork = pr.list_fork_releases()
        assert [r["tag"] for r in fork] == ["v1.0.0"]

    def test_unmarked_and_nonstable_releases_excluded(self, monkeypatch):
        body = "\n".join(
            json.dumps(r)
            for r in [
                {"tag_name": "v1.1.0", "draft": False, "prerelease": False, "body": "no marker"},
                {
                    "tag_name": "0.4.1",
                    "draft": False,
                    "prerelease": False,
                    "body": FORK_RELEASE_MARKER,
                },
                {
                    "tag_name": "v1.2.0rc1",
                    "draft": False,
                    "prerelease": True,
                    "body": FORK_RELEASE_MARKER,
                },
            ]
        )
        self._mock_gh(monkeypatch, body)
        self._mock_git(monkeypatch, {"v1.1.0": "a" * 40, "0.4.1": "b" * 40})

        def fake_run(cmd, capture_output):
            ok = cmd[4] == "a" * 40  # cmd: git merge-base --is-ancestor <base> <sha>
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"", b"")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        assert pr.list_fork_releases() == []


class TestManualMainCheck:
    """C3: the pinned HEAD must be compared against the fetched origin/main."""

    def test_mismatch_refused(self, monkeypatch):
        responses = {
            ("rev-parse", "HEAD"): "1" * 40,
            ("rev-parse", "origin/main"): "2" * 40,
        }
        monkeypatch.setattr(pr, "git", lambda *a: responses.get(a, ""))
        monkeypatch.setattr(pr, "ensure_main_fetched", lambda: None)
        monkeypatch.setattr(
            pr,
            "validate_new_version",
            lambda *a, **k: "v1.0.0",
        )
        with pytest.raises(SystemExit, match="origin/main"):
            pr.cmd_manual(type("A", (), {"version": "1.0.0"})())

    def test_match_accepted(self, monkeypatch, capsys):
        sha = "1" * 40
        monkeypatch.setattr(pr, "git", lambda *a: sha)
        monkeypatch.setattr(pr, "ensure_main_fetched", lambda: None)
        monkeypatch.setattr(pr, "validate_new_version", lambda v, e: "v1.0.0")
        pr.cmd_manual(type("A", (), {"version": "1.0.0"})())
        out = json.loads(capsys.readouterr().out)
        assert out["sha"] == sha
        assert out["tag"] == "v1.0.0"


def pr_capture(fn, args) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


class TestPrepublish:
    """ADV2: full source validation from trusted tooling before target use."""

    def _setup(self, monkeypatch, *, ancestor=True, marker=True, descendant=True):
        tag_sha = "a" * 40
        gh_responses = {
            "ls": "aaaa\trefs/tags/v1.0.0\n",
        }

        def fake_git(*args):
            if args[0] == "ls-remote":
                return gh_responses["ls"]
            if args[0] == "fetch":
                return ""
            if args[0] == "rev-parse":
                return tag_sha + "\n"
            if args[0] == "merge-base":
                raise AssertionError("merge-base must go through subprocess.run")
            raise AssertionError(f"unexpected git call {args}")

        monkeypatch.setattr(pr, "git", fake_git)
        monkeypatch.setattr(pr, "ensure_main_fetched", lambda: None)
        monkeypatch.setattr(pr, "repo_slug", lambda: "geofffranks/utec-py")

        def fake_gh(*args):
            assert args[0] == "api"
            assert args[1] == "repos/geofffranks/utec-py/releases/tags/v1.0.0"
            body = FORK_RELEASE_MARKER if marker else ""
            return json.dumps({"draft": False, "prerelease": False, "body": body})

        monkeypatch.setattr(pr, "gh", fake_gh)

        def fake_run(cmd, capture_output):
            target = cmd[3]
            if target == tag_sha:
                ok = ancestor
            else:  # fork base ancestry
                ok = descendant
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"", b"")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        return tag_sha

    def test_valid_release_passes(self, monkeypatch, capsys):
        self._setup(monkeypatch, ancestor=True, marker=True, descendant=True)
        pr.cmd_prepublish(type("A", (), {"tag": "v1.0.0", "version": "1.0.0"})())
        out = json.loads(capsys.readouterr().out)
        assert out["sha"] == "a" * 40

    def test_non_ancestor_refused(self, monkeypatch):
        self._setup(monkeypatch, ancestor=False)
        with pytest.raises(SystemExit, match="not an ancestor"):
            pr.cmd_prepublish(type("A", (), {"tag": "v1.0.0", "version": "1.0.0"})())

    def test_unmarked_release_refused(self, monkeypatch):
        self._setup(monkeypatch, marker=False)
        with pytest.raises(SystemExit, match="marker"):
            pr.cmd_prepublish(type("A", (), {"tag": "v1.0.0", "version": "1.0.0"})())

    def test_pre_fork_tag_refused(self, monkeypatch):
        self._setup(monkeypatch, ancestor=True, marker=True, descendant=False)
        with pytest.raises(SystemExit, match="strict descendant"):
            pr.cmd_prepublish(type("A", (), {"tag": "v1.0.0", "version": "1.0.0"})())

    def test_tag_version_mismatch_refused(self, monkeypatch):
        self._setup(monkeypatch)
        with pytest.raises(SystemExit, match="does not match"):
            pr.cmd_prepublish(type("A", (), {"tag": "v1.0.0", "version": "1.0.1"})())


class TestNotesSelection:
    """C5: notes come from the previous eligible fork release, not git describe."""

    def test_notes_range_uses_previous_fork_tag(self, monkeypatch, capsys):
        monkeypatch.setattr(
            pr,
            "list_fork_releases",
            lambda: [{"tag": "v1.0.0", "sha": "x"}, {"tag": "v0.9.0", "sha": "y"}],
        )
        monkeypatch.setattr(pr, "git", lambda *a: "abc1 msg\nabc2 msg\n")
        pr.cmd_notes(type("A", (), {"tag": "v1.1.0"})())
        out = capsys.readouterr().out
        assert "Changes since v1.0.0" in out
        # confirm the range passed to git log
        monkeypatch.setattr(pr, "git", lambda *a: f"range={a[2]}\n")
        pr.cmd_notes(type("A", (), {"tag": "v1.1.0"})())
        assert "v1.0.0..v1.1.0" in capsys.readouterr().out

    def test_first_release_uses_fork_base(self, monkeypatch, capsys):
        monkeypatch.setattr(pr, "list_fork_releases", lambda: [])
        monkeypatch.setattr(pr, "git", lambda *a: f"range={a[2]}\n")
        pr.cmd_notes(type("A", (), {"tag": "v1.0.0"})())
        assert f"{DEFAULT_FORK_BASE_COMMIT}..v1.0.0" in capsys.readouterr().out


class TestVerifyAssets:
    """C2: retained release assets must match the attached manifest exactly."""

    def _write(self, tmp_path, files):
        dist = tmp_path / "dist"
        dist.mkdir(exist_ok=True)
        for name, content in files.items():
            (dist / name).write_bytes(content)
        return dist

    def _manifest(self, dist, names):
        lines = []
        for name in names:
            digest = hashlib.sha256((dist / name).read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}")
        (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    def test_valid_manifest_passes(self, tmp_path):
        import verify_assets as va

        dist = self._write(tmp_path, {"a.whl": b"aaa", "b.tar.gz": b"bbb"})
        self._manifest(dist, ["a.whl", "b.tar.gz"])
        assert va.main(["--dir", str(dist), "--manifest", str(dist / "SHA256SUMS")]) == 0

    def test_digest_mismatch_fails(self, tmp_path):
        import verify_assets as va

        dist = self._write(tmp_path, {"a.whl": b"aaa"})
        self._manifest(dist, ["a.whl"])
        (dist / "a.whl").write_bytes(b"changed")
        assert va.main(["--dir", str(dist), "--manifest", str(dist / "SHA256SUMS")]) == 1

    def test_unlisted_and_missing_fail(self, tmp_path):
        import verify_assets as va

        dist = self._write(tmp_path, {"a.whl": b"aaa", "extra.whl": b"ccc"})
        # manifest lists a.whl and a file that does not exist on disk
        digest = hashlib.sha256(b"aaa").hexdigest()
        (dist / "SHA256SUMS").write_text(f"{digest}  a.whl\n{digest}  gone.whl\n")
        assert va.main(["--dir", str(dist), "--manifest", str(dist / "SHA256SUMS")]) == 1


class TestPyPIPreflight:
    """C2: new / identical / conflict classification, staging, and readback."""

    def _files(self, n=2):
        return {f"f{i}.whl": f"content-{i}".encode() for i in range(n)}

    def test_all_new(self, tmp_path, capsys):
        dist = write_dist(tmp_path, self._files())
        monkey_upstream(pp, {})
        assert pp_run_check(dist) == 0
        report = json.loads(capsys.readouterr().out)
        assert len(report["new"]) == 2 and not report["conflict"]

    def test_partial_identical_and_new(self, tmp_path, capsys):
        files = self._files()
        dist = write_dist(tmp_path, files)
        known = {
            "f0.whl": {"digests": {"sha256": _sha(files["f0.whl"])}},
        }
        monkey_upstream(pp, known)
        assert pp_run_check(dist) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["already"] == ["f0.whl"]
        assert report["new"] == ["f1.whl"]

    def test_conflict_aborts(self, tmp_path, capsys):
        dist = write_dist(tmp_path, self._files())
        known = {"f0.whl": {"digests": {"sha256": "0" * 64}}}
        monkey_upstream(pp, known)
        assert pp_run_check(dist) == 1
        report = json.loads(capsys.readouterr().out.splitlines()[0])
        assert report["conflict"] == ["f0.whl"]

    def test_staging_contains_only_new(self, tmp_path):
        files = self._files()
        dist = write_dist(tmp_path, files)
        known = {name: {"digests": {"sha256": _sha(c)}} for name, c in files.items()}
        monkey_upstream(pp, known)
        staging = str(tmp_path / "staging")
        assert pp_run_check(dist, staging=staging) == 3  # nothing new
        assert list((tmp_path / "staging").glob("*")) == []

    def test_staging_partial(self, tmp_path):
        files = self._files()
        dist = write_dist(tmp_path, files)
        known = {"f0.whl": {"digests": {"sha256": _sha(files["f0.whl"])}}}
        monkey_upstream(pp, known)
        staging = tmp_path / "staging"
        assert pp_run_check(dist, staging=str(staging)) == 0
        assert sorted(p.name for p in staging.glob("*")) == ["f1.whl"]

    def test_verify_published_success_counts_already(self, tmp_path, monkeypatch, capsys):
        files = self._files()
        dist = write_dist(tmp_path, files)
        known = {name: {"digests": {"sha256": _sha(c)}} for name, c in files.items()}
        monkey_upstream(pp, known)
        args = argparse_ns(dist)
        assert pp.cmd_verify_published(args) == 0
        summary = json.loads(capsys.readouterr().out)
        assert len(summary["verified"]) == 2

    def test_verify_published_missing_fails(self, tmp_path, monkeypatch, capsys):
        dist = write_dist(tmp_path, self._files())
        monkey_upstream(pp, {})
        args = argparse_ns(dist)
        assert pp.cmd_verify_published(args) == 1
        summary = json.loads(capsys.readouterr().out)
        assert len(summary["missing"]) == 2

    def test_verify_published_wrong_digest_fails(self, tmp_path, monkeypatch, capsys):
        dist = write_dist(tmp_path, self._files())
        known = {f"f{i}.whl": {"digests": {"sha256": "9" * 64}} for i in range(2)}
        monkey_upstream(pp, known)
        args = argparse_ns(dist)
        assert pp.cmd_verify_published(args) == 1
        summary = json.loads(capsys.readouterr().out)
        assert len(summary["wrong_digest"]) == 2


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def monkey_upstream(pp, by_name: dict):
    """Patch fetch_pypi to return the given filename->urls mapping."""

    def fake_fetch(project):
        return dict(by_name)

    pp.fetch_pypi = fake_fetch


def pp_run_check(dist, staging=None):
    args = argparse_ns(dist, staging)
    return pp.cmd_check(args)


def argparse_ns(dist, staging=None):
    import argparse as _ap

    ns = _ap.Namespace(
        dist_dir=dist, project="utec-client", staging_dir=staging, verify_published=False
    )
    return ns
