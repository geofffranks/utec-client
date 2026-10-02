#!/usr/bin/env python3
"""Verify built distributions from a clean environment outside the source tree.

Checks, for the wheel and for a wheel rebuilt from the sdist, installed into a
fresh virtualenv created in a temp directory (never the checkout):
- distribution name is utec-client and version matches the expected value
- the retained API surface imports under utec_client
- no utec_py package or distribution is installed
- the MIT license file is included in the wheel metadata

Usage: check_artifact.py --dist-dir dist --version X.Y.Z
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import venv

EXPECTED_NAME = "utec-client"
API_IMPORTS = [
    "utec_client.api:UHomeApi",
    "utec_client.auth:AbstractAuth",
    "utec_client.devices.light:Light",
    "utec_client.devices.lock:Lock",
    "utec_client.devices.sensor:Sensor",
    "utec_client.devices.switch:Switch",
    "utec_client.exceptions:ApiError",
]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def check_installed(venv_dir: str, artifact: str, expected_version: str, label: str) -> None:
    pip = os.path.join(venv_dir, "bin", "pip")
    python = os.path.join(venv_dir, "bin", "python")
    run([pip, "install", "--no-cache-dir", artifact])
    script = f"""
from importlib.metadata import metadata, version
import importlib

name = metadata("{EXPECTED_NAME}")["Name"]
assert name == "{EXPECTED_NAME}", name
ver = version("{EXPECTED_NAME}")
assert ver == "{expected_version}", ver

mods = []
for spec in {API_IMPORTS!r}:
    module, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(module), attr)
    mods.append((module, attr, obj is not None))
print("imports:", mods)

try:
    import utec_py  # noqa
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("utec_py package must not be installed")

import utec_client
from pathlib import Path
site = Path(utec_client.__file__).parent.parent
di = next(iter(site.glob("utec_client-*.dist-info")), None)
assert di is not None and di.name.startswith("utec_client-"), sorted(p.name for p in site.glob("*.dist-info"))
licenses = list(di.glob("licenses/*"))
assert any("LICENSE" in p.name.upper() for p in licenses), f"no license file in {{di}}: {{licenses}}"
print("license files:", [p.name for p in licenses])
print("CHECK-OK {label}: name={EXPECTED_NAME} version=" + ver)
"""
    result = run([python, "-c", script])
    print(result.stdout)


def build_wheel_from_sdist(sdist: str, out_dir: str) -> str:
    """Build a wheel from the sdist in an isolated venv (proves sdist completeness)."""
    env = tempfile.mkdtemp(prefix="sdist-build-")
    try:
        venv.create(env, with_pip=True)
        pip = os.path.join(env, "bin", "pip")
        run([pip, "install", "--quiet", "build"])
        run(
            [
                os.path.join(env, "bin", "python"),
                "-m",
                "build",
                "--wheel",
                "--outdir",
                out_dir,
                sdist,
            ]
        )
        wheels = glob.glob(os.path.join(out_dir, "*.whl"))
        if not wheels:
            raise SystemExit("wheel rebuilt from sdist produced no wheel")
        return wheels[0]
    finally:
        shutil.rmtree(env, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    wheels = sorted(glob.glob(os.path.join(args.dist_dir, "*.whl")))
    sdists = sorted(glob.glob(os.path.join(args.dist_dir, "*.tar.gz")))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected exactly one wheel and one sdist in {args.dist_dir}, found "
            f"{len(wheels)} wheel(s), {len(sdists)} sdist(s); clean the directory first"
        )
    wheel, sdist = wheels[0], sdists[0]

    for artifact, label in [(wheel, "wheel"), (sdist, "sdist")]:
        env = tempfile.mkdtemp(prefix=f"artifact-check-{label}-")
        try:
            venv.create(env, with_pip=True)
            check_installed(env, artifact, args.version, label)
        finally:
            shutil.rmtree(env, ignore_errors=True)

    rebuilt_dir = tempfile.mkdtemp(prefix="sdist-rebuild-")
    try:
        rebuilt = build_wheel_from_sdist(os.path.abspath(sdist), rebuilt_dir)
        env = tempfile.mkdtemp(prefix="artifact-check-rebuilt-")
        try:
            venv.create(env, with_pip=True)
            check_installed(env, rebuilt, args.version, "wheel-rebuilt-from-sdist")
        finally:
            shutil.rmtree(env, ignore_errors=True)
    finally:
        shutil.rmtree(rebuilt_dir, ignore_errors=True)

    print("ALL ARTIFACT CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())
