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
  13:00 UTC. Takes the highest *eligible fork* release, verifies its tag is
  an ancestor of `main`, and if `main` has any newer commits, cuts the next
  patch (`X.Y.Z+1`) at the current `main` tip. Any new commit counts as a
  change; there is no conventional-commit requirement.
  - With **no eligible fork baseline release yet**, the job skips with a
    notice — it never bootstraps a version from the inherited upstream tags
    (`0.4.1`).
  - If `main` is unchanged since the last fork release, the job skips.
- **Manual releases** (`.github/workflows/manual-release.yml`): dispatch with
  a custom stable `X.Y.Z`. Creates the first fork release and permits
  intentional minor/major choices. Reused, lower, or non-stable versions are
  refused; existing tags are never moved. Dispatching from a non-`main` ref
  is rejected: the workflow always checks out `main` and compares the pinned
  HEAD against the fetched `origin/main` tip before validating.
- **Human releases**: a GitHub release you create in the UI triggers the same
  validated publishing path (`publish.yml` on `release: published`).

### Fork-release eligibility (what counts, and what never can)

Release listing uses the paginated GitHub REST API (`gh api
repos/.../releases --paginate`), which returns release bodies — unlike
`gh release list --json`, which does not.

A release is an **eligible fork release** only when all of these hold:

1. its release body carries the marker line
   `utec-client-fork-release: true`,
2. its tag commit is a **strict descendant of the fork base commit**
   (`7a2c6c35a73b69f3fba683409fb815a79e8906fb`, the commit this fork started
   from; overridable via the `FORK_BASE_COMMIT` environment variable), and
3. for weekly selection, its tag is an ancestor of `main`.

**Boundary:** maintainers can deliberately choose any commit after the fork
base, mark it, and publish it — that is the supported way to set an
intentional first baseline. But an inherited upstream tag can never pass,
no matter what text is added to its release body: inherited tags point at
commits that predate (or are) the fork base, so the strict-descendant check
fails regardless of a forged marker.

## Publishing path

1. **Validation from trusted tooling, before any target-tag use**
   (`publish.yml`, `validate-source` job): runs from a `main` checkout — not
   the release tag — and verifies: strict stable tag/version pair, exactly
   one remote tag ref, tag commit is an ancestor of `origin/main`, release
   is not draft/prerelease, release carries the fork marker, and the tag
   commit is a strict descendant of the fork base. Any failure stops the run
   before the tag's contents are used for anything. All user inputs reach
   scripts through environment variables, never raw shell interpolation.
2. **Artifacts are retained before publication**: the release workflows run
   lint/tests/build on the exact tagged commit, verify the built version
   equals the release version and pass clean-environment artifact checks,
   then attach the wheel, sdist, and a `SHA256SUMS` manifest to the GitHub
   release. Artifacts are never rebuilt at publish time, so a retry always
   uses byte-identical files.
3. **Publication** (`publish.yml`, `pypi-publish` job): downloads the
   retained release assets, verifies every file against the `SHA256SUMS`
   manifest, classifies against PyPI (read-only JSON API): missing → staged
   for upload; identical sha256 → already published (skip); different digest
   → abort, never overwrite. Only the missing files are uploaded, then a
   **post-upload readback** re-queries PyPI and requires every local dist
   file to exist there with an identical sha256.
4. Publication uses PyPI **Trusted Publishing (OIDC)** from the
   `pypi-publish` GitHub environment. No PyPI API token is stored in the
   repository. `id-token: write` exists only in the publish job.

A release created by a workflow with `GITHUB_TOKEN` does **not** trigger
other workflows by its `release` event, so the weekly and manual workflows
explicitly `gh workflow run publish.yml` after creating the tag/release.
A single `concurrency: utec-client-release` group is shared by the weekly,
manual, and publish workflows: it serializes release creation and
publication without nesting (the orchestrator's job finishes before the
dispatched publish run acquires the group), so weekly/manual races cannot
interleave.

## Retries after partial publication

Dispatch `publish.yml` manually with the same `tag` and `version`. It
validates source eligibility exactly as on the first attempt, downloads the
same retained release assets (no rebuild from newer source), skips files
already uploaded with identical hashes, uploads only what is missing, and
verifies the result by readback. Never bump the version to "work around" a
failed publish.

## Owner setup (one-time)

Name checks are provisional until configured on PyPI.

1. **PyPI Trusted Publisher** (required): on pypi.org, create/claim the
   project `utec-client` and add a pending publisher: owner = your GitHub
   owner, repository = this repository's **actual** current name, workflow
   `publish.yml`, environment `pypi-publish`.
2. **GitHub environment `pypi-publish`** (required): create the environment
   and **restrict the deployment branches and tags** to `main` and
   `v*` release tags. This is a required control — the environment must not
   be deployable from arbitrary branches or forks' PR refs. Optionally add
   required human approvers: if enabled, weekly releases pause for approval
   instead of publishing unattended (an explicit tradeoff, not required by
   default). Human approvals are the only optional part; the branch/tag
   source restriction is not.
3. **Actions permissions** (required): default workflow permissions
   read-only; workflows grant `contents: write` / `actions: write` only
   where needed (release creation, asset upload, workflow dispatch), and
   `id-token: write` only in the publish job. Untrusted PR code never runs
   with elevated tokens.
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
  gates and branch/tag restrictions, OIDC trust, and actual PyPI publication
  have not been exercised.
- The PyPI name `utec-client` returned 404 at implementation time but is not
  reserved; a name rejection is an owner follow-up, not something this repo
  can fix.

## Local simulation

Release decisions, asset verification, and PyPI classification are pure
Python and testable without GitHub writes:

```sh
pip install -e .[dev]
pytest tests/test_release_logic.py tests/test_release_plumbing.py
python scripts/prepare_release.py manual v0.5.0                    # validate a version
python scripts/prepare_release.py verify --tag v1.2.3 --version 1.2.3
python scripts/prepare_release.py notes --tag v1.1.0               # notes since prior fork tag
python scripts/pypi_preflight.py --dist-dir dist                   # read-only PyPI check
python scripts/pypi_preflight.py --dist-dir dist --verify-published
python scripts/verify_assets.py --dir dist --manifest dist/SHA256SUMS
```
