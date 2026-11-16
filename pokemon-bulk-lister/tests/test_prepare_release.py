"""Release staging must preserve approved ancestry and exclude local runtime data."""

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_release.py"
SPEC = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo, message):
    git(repo, "add", "--all")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Release test")
    git(repo, "config", "user.email", "release@example.invalid")
    for relative_path in release.REQUIRED_FILES:
        source = repo / release.APP_DIRECTORY / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"committed {relative_path}\n")
    base = commit(repo, "Initial application")
    (repo / "approved-design.txt").write_text("approved design")
    approved = commit(repo, "Approved design")
    (repo / "functional-fix.txt").write_text("preserve this fix")
    canonical = commit(repo, "Necessary functional fix")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", f"HEAD:refs/heads/{release.RELEASE_BRANCH}")
    return repo, base, approved, canonical


def test_archive_preserves_design_and_fixes_without_local_data(checkout, tmp_path):
    repo, _, approved, canonical = checkout
    app = repo / release.APP_DIRECTORY
    for relative_path in (".env", "output/collection.db", "input/private-photo.jpg"):
        local = app / relative_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("local runtime data must not be deployed")
    staging, selected = release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    try:
        assert selected == canonical
        assert not staging.is_relative_to(repo)
        assert (staging / "approved-design.txt").read_text() == "approved design"
        assert (staging / "functional-fix.txt").read_text() == "preserve this fix"
        assert not (staging / ".git").exists()
        for relative_path in release.REQUIRED_FILES:
            assert (staging / release.APP_DIRECTORY / relative_path).read_bytes() == (app / relative_path).read_bytes()
        for relative_path in (".env", "output/collection.db", "input/private-photo.jpg"):
            assert not (staging / release.APP_DIRECTORY / relative_path).exists()
    finally:
        shutil.rmtree(staging)


@pytest.mark.parametrize("staged", [False, True])
def test_uncommitted_tracked_changes_stop_release(checkout, tmp_path, staged):
    repo, _, approved, _ = checkout
    (repo / "functional-fix.txt").write_text("uncommitted change")
    if staged:
        git(repo, "add", "functional-fix.txt")
    with pytest.raises(release.ReleaseError, match="Commit or stash tracked changes"):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    assert not list(tmp_path.glob("pokemon-release-*"))


def test_branch_without_approved_design_cannot_release(checkout, tmp_path):
    repo, base, approved, _ = checkout
    git(repo, "checkout", "--detach", base)
    with pytest.raises(release.ReleaseError, match="does not include the approved design"):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)


def test_fetch_detects_new_canonical_changes_missing_from_head(checkout, tmp_path):
    repo, _, approved, canonical = checkout
    (repo / "new-claude-fix.txt").write_text("must preserve the latest change")
    newer = commit(repo, "New canonical fix")
    git(repo, "push", "origin", f"HEAD:refs/heads/{release.RELEASE_BRANCH}")
    git(repo, "checkout", "--detach", canonical)
    tracking_ref = f"refs/remotes/origin/{release.RELEASE_BRANCH}"
    git(repo, "update-ref", tracking_ref, canonical)
    with pytest.raises(release.ReleaseError, match="must equal the latest canonical branch"):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    assert git(repo, "rev-parse", tracking_ref) == newer
    assert not list(tmp_path.glob("pokemon-release-*"))


def test_unpushed_local_commit_cannot_release(checkout, tmp_path):
    repo, _, approved, _ = checkout
    (repo / "unreviewed.txt").write_text("not on the canonical branch yet")
    commit(repo, "Local changes")
    with pytest.raises(release.ReleaseError, match="must equal the latest canonical branch"):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    assert not list(tmp_path.glob("pokemon-release-*"))


def test_unreachable_remote_does_not_release_stale_tracking_ref(checkout, tmp_path):
    repo, _, approved, _ = checkout
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing-remote.git"))
    with pytest.raises(release.ReleaseError):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    assert not list(tmp_path.glob("pokemon-release-*"))


def test_missing_design_asset_cleans_failed_staging(checkout, tmp_path):
    repo, _, approved, _ = checkout
    (repo / release.APP_DIRECTORY / "webapp/static/design.css").unlink()
    commit(repo, "Accidentally remove theme")
    git(repo, "push", "origin", f"HEAD:refs/heads/{release.RELEASE_BRANCH}")
    with pytest.raises(release.ReleaseError, match="missing application source.*design.css"):
        release.prepare_release(repo, approved_design=approved, staging_parent=tmp_path)
    assert not list(tmp_path.glob("pokemon-release-*"))


def test_staging_inside_checkout_is_rejected(checkout):
    repo, _, approved, _ = checkout
    with pytest.raises(release.ReleaseError, match="outside the checkout"):
        release.prepare_release(repo, approved_design=approved, staging_parent=repo)
    assert not list(repo.glob("pokemon-release-*"))
