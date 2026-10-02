# Releases

How releases work in this fork, and the one-time owner setup required before
the first publish. Nothing below has been exercised against the real GitHub
repository or PyPI yet; every hosted step is listed under "Unverified".

## Model

- **Versioning**: versions come from Git tags via `setuptools-scm`. A stable
  tag `vX.Y.Z` produces package version `X.Y.Z` in both sdist and wheel.
  Untagged builds produce development versions (`X.Y.Z.devN`) and are never
  published by release automation. There are no weekly version-bump commits.
- **Weekly releases** (`.github/workflows/weekly-release.yml`): Mondays
  13:00 UTC. Takes the highest published *fork* release, verifies its tag is
  an ancestor of `main`, and if `main` has any newer commits, cuts the next
  patch (`X.Y.Z+1`) at the current `main` tip. Any new commit counts as a
  change; there is no conventional-commit requirement.
  - With **no fork baseline release yet**, the job skips with a notice — it
    never bootstraps a version from the inherited upstream tags (`0.4.1`).
  - If `main` is unchanged since the last fork release, the job skips.
- **Manual releases** (`.github/workflows/manual-release.yml`): dispatch with
  a custom stable `X.Y.Z`. Creates the first fork release and permits
  intentional minor/major choices. Reused, lower, or non-stable versions are
  refused; existing tags are never moved.
- **Human releases**: a GitHub release you create in the UI triggers the same
  validated publishing path (`publish.yml` on `release: published`).

Fork releases carry the marker line `utec-client-fork-release: true` in the
release body; weekly selection only considers marked releases, so inherited
upstream releases/tags are excluded by construction.

## Publishing path (both automated and human)

1. `publish.yml` checks out the **exact tag**, validates the tag/version
   pair, runs Ruff, tests, build, and clean-environment artifact checks, and
   confirms the built artifact version equals the release version. Any
   failure blocks publication.
2. A read-only PyPI preflight (`scripts/pypi_preflight.py`) classifies files:
   new → upload; identical hash → skip (previous retry already succeeded);
   different hash → abort, never overwrite.
3. Publication uses PyPI **Trusted Publishing (OIDC)** from the
   `pypi-publish` GitHub environment. No PyPI API token is stored in the
   repository. `id-token: write` exists only in the publish job.

A release created by a workflow with `GITHUB_TOKEN` does **not** trigger
other workflows by its `release` event, so the weekly and manual workflows
explicitly `gh workflow run publish.yml` after creating the tag/release.
A `concurrency: utec-client-release` group serializes weekly, manual, and
publishing runs so they cannot race.

## Retries after partial publication

The tag and release always exist before any upload, and artifacts are built
from the tagged commit only. To retry after a failed or partial upload,
dispatch `publish.yml` manually with the same `tag` and `version` — it
rebuilds from the same tag and the preflight skips files already uploaded
with identical hashes and aborts on conflicts. Never bump the version to
"work around" a failed publish.

## Owner setup (one-time)

Name checks are provisional until configured on PyPI.

1. **PyPI Trusted Publisher**: on pypi.org, create/claim the project
   `utec-client` and add a pending publisher (or configure it after the first
   manual upload if the name is taken by squatting rules):
   - owner: your GitHub owner, repository: this repository's **actual**
     current name, workflow: `publish.yml`, environment: `pypi-publish`.
2. **GitHub environment**: create environment `pypi-publish`; optionally
   restrict it to the `main` branch/tags and add required human approvers
   (if enabled, weekly releases pause for approval instead of publishing
   unattended — an explicit tradeoff, not required).
3. **Actions permissions**: default workflow permissions read-only;
   workflows grant `contents: write` / `actions: write` only where needed
   (release creation/dispatch), and `id-token: write` only in the publish
   job. Untrusted PR code never runs with elevated tokens.
4. **Branch protection (optional)**: require the `Tests` workflow checks
   (`lint`, `test (3.11..3.14)`, `package`) on `main`.
5. **First release**: choose the first version yourself and run
   *Manual release* with it (e.g. `1.0.0` or `0.3.0`). Weekly automation
   takes over from that baseline.
6. **Optional rename**: if you rename the repository to
   `geofffranks/utec-client`, update `[project.urls]` in `pyproject.toml`,
   the git remote, and the PyPI Trusted Publisher entries. Release-critical
   configuration intentionally points at the current owned repository until
   a rename actually happens.

## Unverified (hosted-only)

- Scheduled weekly runs, `gh workflow run` dispatch, environment approval
  gates, OIDC trust, and actual PyPI publication have not been exercised.
- The PyPI name `utec-client` returned 404 at implementation time but is not
  reserved; a name rejection is an owner follow-up, not something this repo
  can fix.

## Local simulation

Release decisions are pure Python and testable without GitHub:

```sh
pip install -e .[dev]
pytest tests/test_release_logic.py            # decision logic
python scripts/prepare_release.py manual v0.5.0     # validate a version
python scripts/prepare_release.py verify --tag v1.2.3 --version 1.2.3
python scripts/pypi_preflight.py --dist-dir dist    # read-only PyPI check
```
