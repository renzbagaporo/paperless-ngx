"""Release gates exercised with temporary Git history and mocked GitHub runs."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import personal_release as policy

REPOSITORY = "renzbagaporo/paperless-ngx"
TAG = "paperless-3.3.4-1"


class PersonalReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "--initial-branch=personal")
        version = self.root / "src/paperless/version.py"
        version.parent.mkdir(parents=True)
        version.write_text("__version__: tuple[int, int, int] = (3, 3, 4)\n")
        (self.root / "Dockerfile.custom").write_text(
            "FROM ghcr.io/paperless-ngx/paperless-ngx:3.3.4@sha256:" + "a" * 64 + "\n",
        )
        self.commit("Upstream stable")
        self.stable = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/stable", self.stable)
        (self.root / "fixture.txt").write_text("Personal changes\n")
        self.commit("Personal additions")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/personal", self.sha)
        self.git("tag", TAG)
        self.event = {
            "action": "published",
            "release": {"tag_name": TAG, "draft": False, "prerelease": False},
        }
        self.runs = {
            workflow: [
                {
                    "head_sha": self.sha,
                    "head_branch": "personal",
                    "head_repository": {"full_name": REPOSITORY},
                    "event": "push",
                    "path": f".github/workflows/{workflow}",
                    "run_number": 10,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
            for workflow in policy.WORKFLOWS
        }

    def git(self, *args):
        return policy.git(self.root, *args)

    def commit(self, message):
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            message,
        )

    def verify(self):
        return policy.verify_release(
            self.root,
            self.event,
            self.sha,
            REPOSITORY,
            self.runs.__getitem__,
        )

    def test_tested_personal_release_is_approved(self):
        self.assertEqual(
            self.verify(),
            {"sha": self.sha, "tag": TAG, "image": f"ghcr.io/{REPOSITORY}"},
        )

    def test_annotated_tags_are_resolved_to_the_tested_commit(self):
        self.git("tag", "-d", TAG)
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "tag",
            "-a",
            TAG,
            "-m",
            "Test release",
        )
        self.assertEqual(self.verify()["sha"], self.sha)

    def test_drafts_prereleases_and_other_events_are_rejected(self):
        for field, value in [("draft", True), ("prerelease", True), ("draft", None)]:
            with self.subTest(field=field):
                self.event["release"][field] = value
                with self.assertRaises(policy.ReleaseError):
                    self.verify()
                self.event["release"][field] = False
        self.event["action"] = "edited"
        with self.assertRaises(policy.ReleaseError):
            self.verify()

    def test_only_safe_final_release_tags_are_accepted(self):
        for tag in [
            "v3.3.4",
            "personal-v3.3.4-1",
            "paperless-3.3.4.r3",
            "paperless-3.3.4-0",
            "paperless-3.3.4-01",
            "paperless-3.3.4-1\nsha=bad",
            "paperless-3.3.4-rc1",
            "../paperless-3.3.4-1",
        ]:
            with self.subTest(tag=tag):
                self.event["release"]["tag_name"] = tag
                with self.assertRaises(policy.ReleaseError):
                    self.verify()

    def test_a_moved_tag_is_rejected(self):
        self.git("tag", "-f", TAG, self.stable)
        with self.assertRaisesRegex(policy.ReleaseError, "tag moved"):
            self.verify()

    def test_a_commit_outside_personal_is_rejected(self):
        self.git("update-ref", "refs/remotes/origin/personal", self.stable)
        with self.assertRaises(policy.ReleaseError):
            self.verify()

    def test_stable_must_be_merged_into_the_release(self):
        (self.root / "fixture.txt").write_text("Unmerged stable changes\n")
        self.commit("Next stable")
        self.git(
            "update-ref",
            "refs/remotes/origin/stable",
            self.git("rev-parse", "HEAD"),
        )
        with self.assertRaises(policy.ReleaseError):
            self.verify()

    def test_tag_version_must_match_application_and_stable(self):
        tag = "paperless-3.3.5-1"
        self.git("tag", tag)
        self.event["release"]["tag_name"] = tag
        with self.assertRaisesRegex(policy.ReleaseError, "version must match"):
            self.verify()

    def test_runtime_must_be_pinned_to_the_matching_upstream_release(self):
        (self.root / "Dockerfile.custom").write_text(
            "FROM ghcr.io/paperless-ngx/paperless-ngx:3.3.3@sha256:" + "b" * 64 + "\n",
        )
        self.commit("Mismatched runtime")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/personal", self.sha)
        self.git("tag", "-f", TAG)
        with self.assertRaisesRegex(policy.ReleaseError, "runtime"):
            self.verify()

    def test_every_required_workflow_must_have_checks(self):
        for workflow in policy.WORKFLOWS:
            with self.subTest(workflow=workflow):
                old = self.runs[workflow]
                self.runs[workflow] = []
                with self.assertRaisesRegex(policy.ReleaseError, "no push checks"):
                    self.verify()
                self.runs[workflow] = old

    def test_incomplete_failed_and_skipped_checks_block_publishing(self):
        run = self.runs[policy.WORKFLOWS[0]][0]
        for status, conclusion in [
            ("in_progress", None),
            ("queued", None),
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", "skipped"),
            ("completed", "neutral"),
        ]:
            with self.subTest(status=status, conclusion=conclusion):
                run.update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(policy.ReleaseError, "not passed"):
                    self.verify()

    def test_checks_from_another_context_cannot_approve_the_commit(self):
        workflow = policy.WORKFLOWS[0]
        original = self.runs[workflow][0]
        for changes in [
            {"event": "pull_request"},
            {"event": "workflow_dispatch"},
            {"head_sha": self.stable},
            {"head_branch": "topic"},
            {"head_repository": {"full_name": "other/paperless-ngx"}},
            {"head_repository": None},
            {"path": ".github/workflows/unrelated.yml"},
        ]:
            with self.subTest(changes=changes):
                self.runs[workflow] = [{**original, **changes}]
                with self.assertRaisesRegex(policy.ReleaseError, "not from"):
                    self.verify()

    def test_latest_failed_attempt_cannot_be_hidden_by_an_older_success(self):
        workflow = policy.WORKFLOWS[0]
        success = self.runs[workflow][0]
        for changes in [
            {"run_number": 11, "conclusion": "failure"},
            {"run_attempt": 2, "status": "in_progress", "conclusion": None},
        ]:
            with self.subTest(changes=changes):
                self.runs[workflow] = [success, {**success, **changes}]
                with self.assertRaisesRegex(policy.ReleaseError, "not passed"):
                    self.verify()

    def test_a_successful_new_run_supersedes_an_older_failure(self):
        workflow = policy.WORKFLOWS[0]
        success = self.runs[workflow][0]
        self.runs[workflow] = [
            {**success, "run_number": 9, "conclusion": "failure"},
            success,
        ]
        self.assertEqual(self.verify()["sha"], self.sha)

    def test_main_emits_only_verified_outputs(self):
        event = self.root / "event.json"
        event.write_text(json.dumps(self.event))
        output = self.root / "outputs"
        summary = self.root / "summary"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_REPOSITORY": REPOSITORY,
                    "GITHUB_SHA": self.sha,
                    "GITHUB_TOKEN": "synthetic-test-token",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            ),
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(
                policy,
                "workflow_runs",
                side_effect=lambda *a: self.runs[a[-1]],
            ),
        ):
            self.assertEqual(policy.main(), 0)
            self.assertIn("sha=" + self.sha, output.read_text())
            output.unlink()
            self.runs[policy.WORKFLOWS[0]] = []
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(policy.main(), 1)
            self.assertFalse(output.exists())


class GitHubAPITests(unittest.TestCase):
    def test_queries_are_scoped_to_the_commit_and_personal_pushes(self):
        response = io.BytesIO(json.dumps({"workflow_runs": []}).encode())
        with mock.patch.object(
            policy.urllib.request,
            "urlopen",
            return_value=response,
        ) as request:
            self.assertEqual(
                policy.workflow_runs(REPOSITORY, "a" * 40, "synthetic", "ci-lint.yml"),
                [],
            )
        url = request.call_args.args[0].full_url
        self.assertIn("head_sha=" + "a" * 40, url)
        self.assertIn("branch=personal", url)
        self.assertIn("event=push", url)

    def test_api_failures_omit_credentials_and_response_bodies(self):
        error = urllib.error.HTTPError(
            "https://api.github.com",
            403,
            "denied",
            {},
            None,
        )
        with mock.patch.object(policy.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(policy.ReleaseError, "HTTP 403") as caught:
                policy.workflow_runs(
                    REPOSITORY,
                    "a" * 40,
                    "synthetic-test-token",
                    "ci-lint.yml",
                )
        self.assertNotIn("synthetic-test-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
