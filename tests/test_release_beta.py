# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A beta must reach only the people who go and download it.

packaging/release-beta.sh tags a commit of dev, and the build workflow of
that commit publishes the tag. If the workflow made a beta tag a normal
release, it would become "Latest" and every installed MyEditor would offer
it as an update. So the workflow's settings are pinned here, and the script
is run for real against a throwaway repository with an origin of its own.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = pathlib.Path("packaging") / "release-beta.sh"
WORKFLOW = pathlib.Path(".github") / "workflows" / "build-installers.yml"

needs_posix = pytest.mark.skipif(
    os.name == "nt", reason="the release script runs on macOS and Linux")


# -- the workflow ------------------------------------------------------------

def workflow_step(name):
    """The lines of one named step of the build workflow."""
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    _, found, rest = text.partition(f"      - name: {name}\n")
    assert found, f"no step named {name!r}"
    return [line.strip() for line in rest.split("\n      - name: ")[0].splitlines()]


def test_a_beta_tag_is_a_pre_release_that_is_never_latest():
    step = workflow_step("Create / update release")
    assert "prerelease: ${{ contains(github.ref_name, '-beta') }}" in step
    # Empty for any other tag: the action then leaves "Latest" to GitHub.
    assert ("make_latest: ${{ contains(github.ref_name, '-beta') && 'false' || '' }}"
            in step)


def test_a_beta_leaves_the_install_guide_alone():
    step = workflow_step("Refresh the install guide")
    assert "if: ${{ !contains(github.ref_name, '-beta') }}" in step


# -- the script --------------------------------------------------------------

class Repo:
    """A clone with a dev branch, and the bare repository it pushes to."""

    def __init__(self, folder):
        self.origin = folder / "origin.git"
        self.work = folder / "work"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org",
        }
        self.git("init", "--quiet", "--bare", str(self.origin), cwd=folder)
        self.git("clone", "--quiet", str(self.origin), str(self.work), cwd=folder)
        self.git("checkout", "--quiet", "-b", "dev")
        for path in (SCRIPT, WORKFLOW):
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, self.work / path)
        self.first = self.commit_version("4.0.0")
        self.git("push", "--quiet", "origin", "dev")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.work, env=self.env, check=True,
            capture_output=True, text=True).stdout.strip()

    def commit_version(self, version):
        (self.work / "constants.py").write_text(
            f'APP_VERSION = "{version}"\n', encoding="utf-8")
        return self.commit(f"Version {version}")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", message)
        return self.git("rev-parse", "HEAD")

    def release(self, *args):
        return subprocess.run(
            ["bash", str(self.work / SCRIPT), *args], cwd=self.work, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def refs(self, where):
        """Every ref of the clone or of origin, with the commit it names."""
        listed = self.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=where)
        return dict(line.split() for line in listed.splitlines())

    def tags(self, where):
        return {name: commit for name, commit in self.refs(where).items()
                if name.startswith("refs/tags/")}

    def assert_refused(self, result, reason):
        assert result.returncode == 1, result.stdout + result.stderr
        assert reason in result.stderr
        assert self.tags(self.origin) == {}


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@needs_posix
@pytest.mark.parametrize("version", ["4.0.0-beta1", "v4.0.0-beta1"])
def test_it_tags_dev_and_pushes_nothing_else(repo, version):
    before = repo.refs(repo.origin)
    repo.git("commit", "--quiet", "--allow-empty", "--message", "Not pushed")
    head = repo.git("rev-parse", "HEAD")
    (repo.work / "constants.py").write_text("open change\n", encoding="utf-8")
    (repo.work / "new.txt").write_text("untracked\n", encoding="utf-8")
    status = repo.git("status", "--porcelain")

    result = repo.release(version, "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    tag = {"refs/tags/v4.0.0-beta1": repo.first}
    assert repo.refs(repo.origin) == {**before, **tag}
    assert repo.tags(repo.work) == tag
    assert repo.git("rev-parse", "HEAD") == head
    assert repo.git("branch", "--show-current") == "dev"
    assert repo.git("status", "--porcelain") == status


@needs_posix
def test_without_a_commit_it_takes_what_origin_has_now(repo, tmp_path):
    other = tmp_path / "other"
    repo.git("clone", "--quiet", "--branch", "dev", str(repo.origin), str(other))
    repo.git("commit", "--quiet", "--allow-empty", "--message", "Newer", cwd=other)
    repo.git("push", "--quiet", "origin", "dev", cwd=other)
    newest = repo.git("rev-parse", "HEAD", cwd=other)

    result = repo.release("4.0.0-beta1", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.tags(repo.origin) == {"refs/tags/v4.0.0-beta1": newest}


@needs_posix
def test_a_named_commit_of_dev_is_tagged_as_it_is(repo):
    repo.commit_version("4.1.0")
    repo.git("push", "--quiet", "origin", "dev")

    result = repo.release("4.0.0-beta2", repo.first[:10], "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 newer commit(s)" in result.stdout
    assert repo.tags(repo.origin) == {"refs/tags/v4.0.0-beta2": repo.first}


@needs_posix
@pytest.mark.parametrize("version", [
    "4.0.0", "4.0", "4.0-beta1", "4.0.0-beta", "4.0.0-beta0", "4.0.0-beta1.1",
    "4.0.0-rc1", "4.0.0-beta1 ",
])
def test_only_betas_are_taken(repo, version):
    repo.assert_refused(repo.release(version, "--yes"), "is not a beta version")


@needs_posix
@pytest.mark.parametrize("args", [[], ["4.0.0-beta1", "dev", "more"], ["--force"]])
def test_wrong_arguments_show_how_to_call_it(repo, args):
    result = repo.release(*args)
    assert result.returncode == 2
    assert "usage:" in result.stderr


@needs_posix
def test_the_version_of_the_commit_must_match(repo):
    repo.assert_refused(repo.release("4.1.0-beta1", "--yes"), "APP_VERSION")


@needs_posix
def test_a_commit_that_is_not_on_dev_is_refused(repo):
    repo.git("commit", "--quiet", "--allow-empty", "--message", "Not pushed")
    repo.assert_refused(
        repo.release("4.0.0-beta1", "HEAD", "--yes"), "is not on origin/dev")


@needs_posix
def test_an_unknown_commit_is_refused(repo):
    repo.assert_refused(
        repo.release("4.0.0-beta1", "nothing", "--yes"), "there is no commit named")


@needs_posix
@pytest.mark.parametrize("setting", ["prerelease", "make_latest"])
def test_a_commit_whose_workflow_would_publish_it_as_latest_is_refused(repo, setting):
    workflow = repo.work / WORKFLOW
    lines = workflow.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.strip().startswith(f"{setting}:")]
    assert len(kept) == len(lines) - 1
    workflow.write_text("".join(kept), encoding="utf-8")
    repo.commit("A workflow from before betas")
    repo.git("push", "--quiet", "origin", "dev")

    repo.assert_refused(
        repo.release("4.0.0-beta1", "--yes"), f"does not set {setting} for beta tags")


@needs_posix
def test_a_tag_that_exists_locally_is_left_alone(repo):
    repo.git("tag", "v4.0.0-beta1")
    repo.assert_refused(
        repo.release("4.0.0-beta1", "--yes"), "already exists locally")
    assert repo.tags(repo.work) == {"refs/tags/v4.0.0-beta1": repo.first}


@needs_posix
def test_a_tag_that_exists_on_origin_is_left_alone(repo):
    repo.git("tag", "v4.0.0-beta1", repo.first, cwd=repo.origin)

    result = repo.release("4.0.0-beta1", "--yes")

    assert result.returncode == 1
    assert "already exists on origin" in result.stderr
    assert repo.tags(repo.work) == {}


@needs_posix
def test_without_an_answer_nothing_is_tagged(repo):
    repo.assert_refused(repo.release("4.0.0-beta1"), "without --yes")
    assert repo.tags(repo.work) == {}


@needs_posix
def test_an_unreachable_origin_stops_it(repo):
    repo.origin.rename(repo.origin.with_name("gone.git"))

    result = repo.release("4.0.0-beta1", "--yes")

    assert result.returncode == 1
    assert "could not fetch dev" in result.stderr
    assert repo.tags(repo.work) == {}


@needs_posix
def test_a_refused_push_leaves_no_local_tag(repo):
    hook = repo.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    repo.assert_refused(
        repo.release("4.0.0-beta1", "--yes"), "could not push tag")
    assert repo.tags(repo.work) == {}
