#!/usr/bin/env python3
"""Prepare committed, current application sources for a production deployment.

Run from any directory. The only stdout output is a new staging directory;
deploy from that repository root, then remove it when finished. This script
does not deploy, copy local configuration, or read deployment credentials.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


APP_DIRECTORY = Path("pokemon-bulk-lister")
APPROVED_DESIGN = "1ca010c2e1fa20f260399e36317e0d9c2c7a09bd"
RELEASE_BRANCH = "claude/dreamy-archimedes-7c38oi"
REQUIRED_FILES = (
    "app.py",
    "vercel.json",
    "requirements.txt",
    "webapp/app.py",
    "webapp/static/app.js",
    "webapp/static/design.css",
    "webapp/templates/_navigation.html",
)


class ReleaseError(RuntimeError):
    """The checkout cannot safely provide a release."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode:
        raise ReleaseError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def prepare_release(
    repo: Path,
    *,
    approved_design: str = APPROVED_DESIGN,
    release_branch: str = RELEASE_BRANCH,
    staging_parent: Path | None = None,
) -> tuple[Path, str]:
    """Fetch the release branch and archive a validated HEAD, excluding local files.

    The injectable refs support isolated Git fixtures. The CLI always uses the
    approved design and canonical branch above, with no bypass switches.
    """
    repo = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("Commit or stash tracked changes before preparing a release.")

    canonical_ref = f"refs/remotes/origin/{release_branch}"
    git(repo, "fetch", "--no-tags", "origin", f"refs/heads/{release_branch}:{canonical_ref}")
    head = git(repo, "rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", approved_design, head],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise ReleaseError(
            f"HEAD does not include the approved design ({approved_design}). "
            "Merge it and resolve conflicts before releasing."
        )
    if head != git(repo, "rev-parse", canonical_ref):
        raise ReleaseError(
            f"HEAD must equal the latest canonical branch ({canonical_ref}). "
            "Merge and push the reviewed changes, then check out that commit."
        )

    staging = Path(tempfile.mkdtemp(prefix="pokemon-release-", dir=staging_parent)).resolve()
    try:
        if staging.is_relative_to(repo):
            raise ReleaseError("The staging directory must be outside the checkout; change TMPDIR.")
        with tempfile.TemporaryFile() as archive:
            result = subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", head],
                stdout=archive, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode:
                raise ReleaseError(result.stderr.strip() or "Could not archive HEAD.")
            archive.seek(0)
            with tarfile.open(fileobj=archive) as contents:
                contents.extractall(staging, filter="data")
        for relative_path in REQUIRED_FILES:
            source = staging / APP_DIRECTORY / relative_path
            if not source.is_file() or not source.resolve().is_relative_to(staging):
                raise ReleaseError(f"Release is missing application source: {APP_DIRECTORY / relative_path}")
    except BaseException:
        shutil.rmtree(staging)
        raise
    return staging, head


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        staging, head = prepare_release(Path(__file__).resolve().parents[2])
    except (ReleaseError, OSError, tarfile.TarError) as error:
        print(f"Release preparation stopped: {error}", file=sys.stderr)
        return 1
    print(f"Prepared committed release {head}; deploy from the staging root below.", file=sys.stderr)
    print(staging)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
