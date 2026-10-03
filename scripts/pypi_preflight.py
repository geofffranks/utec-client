#!/usr/bin/env python3
"""Read-only PyPI preflight and post-upload readback for safe publication.

Queries the public PyPI JSON API (GETs only, no writes) for utec-client.

Modes:
  check (default)
    Classifies the local dist files in --dist-dir against PyPI:
      - file missing on PyPI          -> "new"      (safe to upload)
      - file present, same sha256     -> "already"  (previous retry succeeded)
      - file present, other digest    -> "conflict" (abort; never overwrite)
    Files classified "new" are copied to --staging-dir so the publish step can
    upload only missing files without --skip-existing masking conflicts.
    Exit codes: 0 = proceed (some files new), 3 = already fully published,
    1 = conflict or error.

  verify-published
    Post-upload readback: every local dist file must now exist on PyPI with an
    identical sha256 digest. Exit 0 only when all files verified; skipped
    ("already") files count as verified. Prints a JSON summary on stdout.

  check-files-exist
    Version-scoped (requires --version): exit 0 when the SELECTED version
    already has files on PyPI, 1 when it has none (404 on the version JSON
    endpoint; a project-level 404 is reported distinctly), 4 when PyPI state
    cannot be determined. Used by the publish build job for the fail-closed
    rule: after any files of the SELECTED version are on PyPI, a missing or
    expired validated artifact blocks publication — rebuilding that version
    is only allowed while it has no files on PyPI. Older published versions
    never block a new release.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/{project}/json"
PYPI_VERSION_JSON = "https://pypi.org/pypi/{project}/{version}/json"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


DISTRIBUTION_SUFFIXES = (".whl", ".tar.gz")


def local_dists(dist_dir: str) -> dict[str, str]:
    """Digests of the distribution files only (wheel and sdist).

    Strict filtering by distribution suffix: auxiliary files that may live in
    dist/ (e.g. the SHA256SUMS manifest attached to release assets) are never
    treated as publishable distributions, so staging and the post-upload
    readback never demand them on PyPI.
    """
    files = {
        os.path.basename(p): p
        for p in sorted(glob.glob(os.path.join(dist_dir, "*")))
        if os.path.isfile(p) and p.endswith(DISTRIBUTION_SUFFIXES)
    }
    if not files:
        raise SystemExit(f"no .whl or .tar.gz distribution files in {dist_dir}")
    return {name: sha256_of(path) for name, path in files.items()}


def fetch_pypi(project: str) -> dict[str, dict]:
    try:
        with urllib.request.urlopen(PYPI_JSON.format(project=project), timeout=30) as resp:
            remote = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}  # project not on PyPI yet
        raise
    return {u["filename"]: u for u in remote.get("urls", [])}


def classify(local: dict[str, str], remote: dict[str, dict]) -> dict[str, list[str]]:
    report = {"new": [], "already": [], "conflict": []}
    for name, digest in local.items():
        info = remote.get(name)
        if info is None:
            report["new"].append(name)
        elif info["digests"].get("sha256") == digest:
            report["already"].append(name)
        else:
            report["conflict"].append(name)
    return report


def cmd_check(args: argparse.Namespace) -> int:
    local = local_dists(args.dist_dir)
    report = classify(local, fetch_pypi(args.project))
    print(json.dumps(report))

    if report["conflict"]:
        print(
            "::error::PyPI already has file(s) with different content: "
            + ", ".join(report["conflict"])
            + " — refusing to publish; do not reuse this version.",
            file=sys.stderr,
        )
        return 1

    if args.staging_dir:
        shutil.rmtree(args.staging_dir, ignore_errors=True)
        os.makedirs(args.staging_dir, exist_ok=True)
        for name in report["new"]:
            shutil.copy2(os.path.join(args.dist_dir, name), os.path.join(args.staging_dir, name))

    if not report["new"]:
        print(
            "::notice::all files already published with identical hashes; nothing to upload",
            file=sys.stderr,
        )
        return 3
    return 0


def project_has_files(project: str) -> bool:
    """True when the project already has any release files on PyPI (N5)."""
    return bool(fetch_pypi(project))


def fetch_pypi_version(project: str, version: str) -> dict | None:
    """Version JSON for one release, or None on HTTP 404.

    404 covers both "project not on PyPI" and "version not published";
    callers that need the distinction re-query the project endpoint. Other
    HTTP errors propagate (the caller must fail closed on unknown state).
    """
    try:
        with urllib.request.urlopen(
            PYPI_VERSION_JSON.format(project=project, version=version), timeout=30
        ) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def cmd_check_files_exist(args: argparse.Namespace) -> int:
    """N6: gate on the SELECTED release's files only, never project-wide.

    Exit codes: 0 = the selected version already has files on PyPI; 1 = it
    has none (rebuilding/revalidating this version is safe); 4 = PyPI state
    could not be determined (transport/API error) — the caller must fail
    closed rather than guess.
    """
    try:
        data = fetch_pypi_version(args.project, args.version)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(
            f"::error::cannot determine PyPI state for {args.project} {args.version}: {exc}; "
            "refusing to decide rebuild eligibility on unknown state",
            file=sys.stderr,
        )
        return 4
    if data is None:
        # Distinguish a 404 for the whole project from a missing version.
        detail = "version-not-published"
        try:
            if not fetch_pypi(args.project):
                detail = "project-not-on-pypi"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                detail = "project-not-on-pypi"
            else:
                print(
                    f"::error::cannot determine PyPI state for {args.project} {args.version}: "
                    f"HTTP {exc.code}; refusing to decide rebuild eligibility on unknown state",
                    file=sys.stderr,
                )
                return 4
        print(
            json.dumps(
                {
                    "project": args.project,
                    "version": args.version,
                    "has_files": False,
                    "detail": detail,
                }
            )
        )
        return 1
    has_files = bool(data.get("urls"))
    print(
        json.dumps(
            {
                "project": args.project,
                "version": args.version,
                "has_files": has_files,
                "detail": "version-published",
            }
        )
    )
    return 0 if has_files else 1


def cmd_verify_published(args: argparse.Namespace) -> int:
    local = local_dists(args.dist_dir)
    remote = fetch_pypi(args.project)
    verified, missing, wrong = [], [], []
    for name, digest in local.items():
        info = remote.get(name)
        if info is None:
            missing.append(name)
        elif info["digests"].get("sha256") == digest:
            verified.append(name)
        else:
            wrong.append(name)
    print(json.dumps({"verified": verified, "missing": missing, "wrong_digest": wrong}))
    if missing or wrong:
        print(
            f"::error::post-upload readback failed: missing={missing} wrong_digest={wrong}",
            file=sys.stderr,
        )
        return 1
    print("::notice::post-upload readback verified all dist files on PyPI", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--project", default="utec-client")
    parser.add_argument(
        "--staging-dir",
        help="copy files classified 'new' into this directory (check mode only)",
    )
    parser.add_argument(
        "--verify-published",
        action="store_true",
        help="post-upload readback: verify every local dist file is on PyPI with matching sha256",
    )
    parser.add_argument(
        "--check-files-exist",
        action="store_true",
        help="exit 0 if the SELECTED --version has files on PyPI, 1 if none, 4 if unknown",
    )
    parser.add_argument(
        "--version", help="release version for --check-files-exist (required there)"
    )
    args = parser.parse_args(argv)
    if args.check_files_exist:
        if not args.version:
            parser.error("--check-files-exist requires --version")
        return cmd_check_files_exist(args)
    if args.verify_published:
        return cmd_verify_published(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
