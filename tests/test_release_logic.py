"""Tests for the release-selection and version-validation logic."""

import pytest

from release_logic import (
    FORK_RELEASE_MARKER,
    ReleaseError,
    is_fork_release,
    parse_stable_version,
    select_weekly_release,
    tag_exists,
    validate_new_version,
    version_tag,
)


class TestParseStableVersion:
    def test_plain_version(self):
        assert parse_stable_version("0.2.4") == (0, 2, 4)

    def test_v_prefixed_tag(self):
        assert parse_stable_version("v1.2.3") == (1, 2, 3)

    @pytest.mark.parametrize(
        "ref",
        ["1.2", "v1.2", "1.2.3.4", "1.2.3-dev1", "1.2.3rc1", "release", "", "v1.2.x"],
    )
    def test_invalid_refs_rejected(self, ref):
        with pytest.raises(ReleaseError):
            parse_stable_version(ref)

    def test_version_tag_normalizes(self):
        assert version_tag("1.2.3") == "v1.2.3"
        assert version_tag("v1.2.3") == "v1.2.3"


class TestForkReleaseMarker:
    def test_marker_present(self):
        body = f"Changes since last release.\n{FORK_RELEASE_MARKER}\n"
        assert is_fork_release(body)

    def test_marker_absent(self):
        assert not is_fork_release("Inherited upstream release 0.4.1")

    def test_marker_must_be_exact(self):
        assert not is_fork_release("utec-client-fork-release: false")


class TestTagExists:
    def test_existing_tag(self):
        assert tag_exists("v1.0.0", ["v1.0.0"])

    def test_missing_tag(self):
        assert not tag_exists("v1.0.1", ["v1.0.0"])


class TestValidateNewVersion:
    def test_higher_patch_accepted(self):
        assert validate_new_version("v1.0.1", ["1.0.0"]) == "v1.0.1"

    def test_intentional_minor_and_major_accepted(self):
        assert validate_new_version("2.0.0", ["1.9.4"]) == "v2.0.0"
        assert validate_new_version("1.1.0", ["1.0.5"]) == "v1.1.0"

    def test_reused_version_refused(self):
        with pytest.raises(ReleaseError, match="already exists"):
            validate_new_version("v1.0.0", ["1.0.0"])

    def test_lower_version_refused(self):
        with pytest.raises(ReleaseError, match="not higher"):
            validate_new_version("0.9.0", ["1.0.0"])

    def test_invalid_version_refused(self):
        with pytest.raises(ReleaseError):
            validate_new_version("1.0.0-beta", [])

    def test_inherited_upstream_version_refused(self):
        with pytest.raises(ReleaseError, match="already exists"):
            validate_new_version("0.4.1", ["0.4.1"])


class TestSelectWeeklyRelease:
    def _release(self, tag, sha):
        return {"tag": tag, "sha": sha}

    def test_no_baseline_skips_with_notice(self):
        decision = select_weekly_release([], "abc", False, 5)
        assert decision.action == "skip"
        assert "baseline" in decision.reason

    def test_unchanged_main_skips(self):
        releases = [self._release("v1.0.0", "abc")]
        decision = select_weekly_release(releases, "abc", True, 0)
        assert decision.action == "skip"
        assert decision.reason.startswith("main has no new commits")

    def test_new_commits_increment_patch(self):
        releases = [self._release("v1.0.0", "abc"), self._release("v0.9.0", "old")]
        decision = select_weekly_release(releases, "def", True, 3)
        assert decision.action == "release"
        assert decision.tag == "v1.0.1"
        assert decision.version == "1.0.1"

    def test_non_ancestor_tag_raises(self):
        releases = [self._release("v1.0.0", "abc")]
        with pytest.raises(ReleaseError, match="not an ancestor"):
            select_weekly_release(releases, "def", False, 3)

    def test_inherited_releases_excluded_upstream_of_this_call(self):
        # The caller filters by the fork marker; a high inherited version must
        # never influence selection. This test documents the contract.
        releases = [self._release("v1.0.0", "abc")]
        assert not is_fork_release("upstream release 0.4.1 body")
        decision = select_weekly_release(releases, "def", True, 1)
        assert decision.tag == "v1.0.1"
