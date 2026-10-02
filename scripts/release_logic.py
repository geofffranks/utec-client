"""Pure release-selection and version-validation logic for utec-client.

Imported by the release workflows (via small runner scripts) and by the
automated tests in tests/test_release_logic.py. All functions are pure and
side-effect free so release decisions can be checked locally without any
GitHub or PyPI access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A fork release carries this marker line in its GitHub release body. Inherited
# upstream releases/tags never carry it, so weekly automation ignores them.
# The marker alone is NOT sufficient eligibility (a collaborator could add it
# to any release); see is_eligible_fork_release.
FORK_RELEASE_MARKER = "utec-client-fork-release: true"

# The commit this fork started from (base of the fork). Tags/releases whose
# commit is not a strict descendant of this commit are inherited upstream
# artifacts and can never be eligible fork releases, even with a marker.
DEFAULT_FORK_BASE_COMMIT = "7a2c6c35a73b69f3fba683409fb815a79e8906fb"

_STABLE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    """Raised when a release version or source is not eligible."""


@dataclass(frozen=True)
class ReleaseDecision:
    action: str  # "release" or "skip"
    tag: str | None = None
    version: str | None = None
    reason: str = ""


def parse_stable_version(ref: str) -> tuple[int, int, int]:
    """Parse a stable version or tag (X.Y.Z or vX.Y.Z).

    Raises ReleaseError for anything else (pre-releases, dev versions, junk).
    """
    m = _STABLE_RE.match(ref.strip())
    if not m:
        raise ReleaseError(f"'{ref}' is not a stable version (expected X.Y.Z or vX.Y.Z)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def version_tag(ref: str) -> str:
    """Normalize a version or tag to the canonical tag form vX.Y.Z."""
    x, y, z = parse_stable_version(ref)
    return f"v{x}.{y}.{z}"


def is_fork_release(release_body: str) -> bool:
    """True when a release body carries the fork-release marker.

    This is the marker check only. Eligibility additionally requires the
    is_eligible_fork_release ancestry checks.
    """
    return any(line.strip() == FORK_RELEASE_MARKER for line in release_body.splitlines())


def is_eligible_fork_release(
    *,
    has_marker: bool,
    base_is_ancestor: bool,
    same_commit: bool,
) -> bool:
    """Decide fork-release eligibility from marker plus ancestry facts.

    A release is an eligible fork release only when all of these hold:
    - its release body carries the fork-release marker, and
    - the fork base commit is a proper ancestor of the release's tag commit
      (strict descendant: the tag is not the base commit itself, and every
      inherited upstream tag predates the fork base).

    Boundary: a maintainer can deliberately mark and publish a release on any
    commit after the fork base — that is the supported way to choose an
    intentional first baseline. But an inherited upstream tag can never pass:
    it either predates the base or IS the base, so the strict-descendant check
    fails regardless of any marker text added to its release body.
    """
    return has_marker and base_is_ancestor and not same_commit


def previous_fork_tag(fork_releases: list[dict], current_tag: str) -> str | None:
    """Highest eligible fork release tag strictly below current_tag, or None."""
    current = parse_stable_version(current_tag)
    lower = [
        r
        for r in fork_releases
        if r["tag"] != version_tag(current_tag) and parse_stable_version(r["tag"]) < current
    ]
    if not lower:
        return None
    return version_tag(max(lower, key=lambda r: parse_stable_version(r["tag"]))["tag"])


def tag_exists(tag: str, existing_tags: list[str]) -> bool:
    """True when the tag already exists. Existing tags are never moved."""
    return tag in existing_tags


def validate_new_version(
    candidate: str,
    existing_versions: list[str],
) -> str:
    """Validate a manually requested stable version and return its canonical tag.

    Rules: must be a stable X.Y.Z, must not already exist, and must be strictly
    greater than every existing version. Intentional minor/major bumps are
    allowed; reused or lower versions are refused.
    """
    cand = parse_stable_version(candidate)
    existing = [parse_stable_version(v) for v in existing_versions]
    for old in existing:
        if old == cand:
            raise ReleaseError(
                f"version {version_tag(candidate)} already exists; tags are never moved"
            )
        if old > cand:
            raise ReleaseError(
                f"version {version_tag(candidate)} is not higher than existing "
                f"version {version_tag('.'.join(map(str, old)))}"
            )
    return version_tag(candidate)


def select_weekly_release(
    fork_releases: list[dict],
    main_head: str,
    is_ancestor: bool,
    commits_since_tag: int,
) -> ReleaseDecision:
    """Decide whether the weekly job should cut the next patch release.

    fork_releases: published fork releases (dicts with "tag" and "sha"), as
      identified by the fork-release marker. Inherited upstream releases are
      excluded by the caller before this is called.
    main_head: commit SHA currently at the tip of main.
    is_ancestor: whether the latest fork release tag is an ancestor of main.
    commits_since_tag: number of commits on main since that tag's commit.

    With no fork baseline release yet, the job skips with an actionable notice
    instead of inventing a version. Any new main commit counts as a change.
    """
    if not fork_releases:
        return ReleaseDecision(
            action="skip",
            reason=(
                "no fork baseline release exists yet; create the first release with the "
                "'manual-release' workflow (custom version) before weekly automation runs"
            ),
        )
    highest = max(fork_releases, key=lambda r: parse_stable_version(r["tag"]))
    tag = version_tag(highest["tag"])
    if not is_ancestor:
        raise ReleaseError(
            f"latest fork release tag {tag} is not an ancestor of main; refusing to build from diverged history"
        )
    if commits_since_tag == 0:
        return ReleaseDecision(action="skip", reason=f"main has no new commits since {tag}")
    x, y, z = parse_stable_version(tag)
    return ReleaseDecision(
        action="release",
        tag=f"v{x}.{y}.{z + 1}",
        version=f"{x}.{y}.{z + 1}",
        reason=f"{commits_since_tag} new commit(s) on main since {tag}",
    )
