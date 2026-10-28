"""Verify a release before publishing its image.

Run from the repository root in the release workflow. GitHub supplies the event,
token and output locations through the runner's environment.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


WORKFLOWS = (
    "ci-backend.yml",
    "ci-frontend.yml",
    "custom-checks.yml",
    "ci-lint.yml",
    "ci-static-analysis.yml",
    "ci-docs.yml",
)
RELEASE_TAG = re.compile(
    r"personal-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-[1-9]\d*",
)


class ReleaseError(Exception):
    """The release does not satisfy the publication policy."""


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise ReleaseError(
            "Release refs are missing or have invalid ancestry",
        ) from error


def version_at(root: Path, revision: str) -> str:
    tree = ast.parse(git(root, "show", f"{revision}:src/paperless/version.py"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
        ):
            value = ast.literal_eval(node.value)
            if (
                isinstance(value, tuple)
                and len(value) == 3
                and all(type(part) is int and part >= 0 for part in value)
            ):
                return ".".join(map(str, value))
    raise ReleaseError("Cannot determine the upstream version")


def verify_ci(
    sha: str,
    repository: str,
    load_runs: Callable[[str], list[dict[str, Any]]],
) -> None:
    for workflow in WORKFLOWS:
        runs = load_runs(workflow)
        if not runs:
            raise ReleaseError(f"{workflow}: no push checks for the release commit")
        latest = max(
            runs,
            key=lambda run: (run["run_number"], run.get("run_attempt", 1)),
        )
        if (
            latest.get("head_sha") != sha
            or latest.get("head_branch") != "personal"
            or latest.get("event") != "push"
            or (latest.get("head_repository") or {}).get("full_name", "").lower()
            != repository.lower()
            or latest.get("path", "").split("@", 1)[0]
            != f".github/workflows/{workflow}"
        ):
            raise ReleaseError(f"{workflow}: checks are not from this personal commit")
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise ReleaseError(f"{workflow}: latest checks have not passed")


def verify_release(
    root: Path,
    event: dict[str, Any],
    sha: str,
    repository: str,
    load_runs: Callable[[str], list[dict[str, Any]]],
) -> dict[str, str]:
    release = event.get("release", {})
    if (
        event.get("action") != "published"
        or release.get("draft") is not False
        or release.get("prerelease") is not False
    ):
        raise ReleaseError("Only published final releases can publish images")
    tag = release.get("tag_name", "")
    match = RELEASE_TAG.fullmatch(tag)
    if match is None or len(tag) > 128:
        raise ReleaseError("Use a release tag such as personal-v3.3.4-1")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        repository,
    ):
        raise ReleaseError("Invalid GitHub repository or commit")
    if git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != sha:
        raise ReleaseError("The release tag moved after the event was created")
    git(root, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/personal")
    git(root, "merge-base", "--is-ancestor", "refs/remotes/origin/stable", sha)
    version = match[1]
    if version_at(root, sha) != version or version_at(root, "origin/stable") != version:
        raise ReleaseError(
            "The tag, application and integrated stable version must match",
        )
    dockerfile = git(root, "show", f"{sha}:Dockerfile.custom")
    if not re.search(
        rf"^FROM ghcr\.io/paperless-ngx/paperless-ngx:{re.escape(version)}@sha256:[0-9a-f]{{64}}$",
        dockerfile,
        re.MULTILINE,
    ):
        raise ReleaseError("Pin Dockerfile.custom to the integrated upstream runtime")
    verify_ci(sha, repository, load_runs)
    return {"sha": sha, "tag": tag, "image": f"ghcr.io/{repository.lower()}"}


def workflow_runs(
    repository: str,
    sha: str,
    token: str,
    workflow: str,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"head_sha": sha, "branch": "personal", "event": "push", "per_page": 100},
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["workflow_runs"]
    except urllib.error.HTTPError as error:
        raise ReleaseError(f"GitHub CI lookup failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ReleaseError("GitHub CI lookup is unavailable") from error


def main() -> int:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        repository = os.environ["GITHUB_REPOSITORY"]
        sha = os.environ["GITHUB_SHA"]
        token = os.environ["GITHUB_TOKEN"]
        result = verify_release(
            Path.cwd(),
            event,
            sha,
            repository,
            lambda workflow: workflow_runs(repository, sha, token, workflow),
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.writelines(f"{key}={value}\n" for key, value in result.items())
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(
                f"Verified release `{result['tag']}` at `{sha}`. "
                f"All {len(WORKFLOWS)} required push workflows passed.\n\n"
                f"Deployment image: `{result['image']}:personal-{sha}`\n",
            )
    except (ReleaseError, KeyError, ValueError) as error:
        sys.stderr.write(f"Release refused: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
