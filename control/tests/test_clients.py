import io
import unittest
import urllib.error
from unittest.mock import patch

from owner_control.clients import (
    GitHubClient,
    SymphonyClient,
    TestEnvironmentClient,
    extract_owner_question,
    request_json,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=10):
        self.calls.append((method, url, body))
        return self.responses.pop(0)


def project_response():
    return {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "status-field",
                            "name": "Status",
                            "options": [
                                {"id": "ready-ai", "name": "Ready for AI"},
                                {"id": "rfa", "name": "Ready for Acceptance"},
                                {"id": "done", "name": "Done"},
                            ],
                        }
                    ]
                },
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "project-item-401",
                            "fieldValues": {
                                "nodes": [
                                    {"name": "Ready for Acceptance", "field": {"name": "Status"}}
                                ]
                            },
                            "content": {
                                "__typename": "Issue",
                                "number": 401,
                                "title": "Ready on TEST",
                                "url": "https://github.test/issues/401",
                                "state": "OPEN",
                                "labels": {"nodes": [{"name": "backend"}]},
                                "comments": {
                                    "nodes": [
                                        {"body": "Progress update"},
                                        {"body": "Owner question: choose A or B?"},
                                    ]
                                },
                                "closedByPullRequestsReferences": {
                                    "nodes": [
                                        {
                                            "number": 99,
                                            "url": "https://github.test/pull/99",
                                            "state": "MERGED",
                                            "merged": True,
                                            "commits": {
                                                "nodes": [
                                                    {
                                                        "commit": {
                                                            "oid": "abc123",
                                                            "statusCheckRollup": {"state": "SUCCESS"},
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                            },
                        }
                    ],
                },
            }
        }
    }


class GitHubClientTest(unittest.TestCase):
    def test_normalizes_project_issue_pr_ci_and_owner_question(self):
        transport = FakeTransport([project_response()])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        project = client.project_snapshot()

        self.assertEqual(
            project["items"][0],
            {
                "number": 401,
                "identifier": "#401",
                "title": "Ready on TEST",
                "url": "https://github.test/issues/401",
                "status": "Ready for Acceptance",
                "state": "OPEN",
                "labels": ["backend"],
                "owner_question": "choose A or B?",
                "project_item_id": "project-item-401",
                "pr": {
                    "number": 99,
                    "url": "https://github.test/pull/99",
                    "state": "MERGED",
                    "merged": True,
                    "sha": "abc123",
                },
                "ci": {"status": "success", "url": "https://github.test/pull/99/checks"},
            },
        )

    def test_set_status_uses_cached_project_item_and_named_option(self):
        transport = FakeTransport([project_response(), {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-401"}}}}])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )
        client.project_snapshot()

        client.set_status(401, "Done")

        mutation = transport.calls[1][2]
        self.assertEqual(
            mutation["variables"],
            {
                "projectId": "project-id",
                "itemId": "project-item-401",
                "fieldId": "status-field",
                "optionId": "done",
            },
        )

    def test_remove_label_accepts_githubs_array_response(self):
        transport = FakeTransport([[]])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        client.remove_label(401, "symphony")

        self.assertEqual(
            transport.calls,
            [
                (
                    "DELETE",
                    "https://api.github.com/repos/qio3/zavod/issues/401/labels/symphony",
                    None,
                )
            ],
        )

    def test_graphql_rate_limit_variant_is_owner_readable(self):
        transport = FakeTransport(
            [
                {
                    "errors": [
                        {
                            "type": "RATE_LIMITED",
                            "message": "API rate limit already exceeded for user ID 123",
                        }
                    ]
                }
            ]
        )
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        with self.assertRaisesRegex(RuntimeError, "GitHub GraphQL rate limit exhausted"):
            client.project_snapshot()

    def test_rest_rate_limit_error_is_owner_readable_without_leaking_response(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/qio3/zavod/git/ref/heads/main",
            403,
            "Forbidden",
            {"X-RateLimit-Remaining": "0"},
            io.BytesIO(b'{"message":"API rate limit exceeded for user ID 123"}'),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError,
                "^GitHub API rate limit exhausted$",
            ):
                request_json(
                    "GET",
                    "https://api.github.com/repos/qio3/zavod/git/ref/heads/main",
                )

    def test_extract_owner_question_requires_explicit_marker(self):
        self.assertEqual(extract_owner_question([{"body": "Вопрос владельцу: какой вариант?"}]), "какой вариант?")
        self.assertIsNone(extract_owner_question([{"body": "ordinary progress"}]))


class RuntimeClientTest(unittest.TestCase):
    def test_symphony_state_rejects_non_object_json(self):
        client = SymphonyClient("http://127.0.0.1:4082", transport=FakeTransport([[]]))

        with self.assertRaisesRegex(RuntimeError, "^Symphony state response must be a JSON object$"):
            client.state()

    def test_test_deployment_rejects_non_object_health_json(self):
        client = TestEnvironmentClient(
            "https://test.example/health",
            "https://test.example",
            transport=FakeTransport([[]]),
        )

        with self.assertRaisesRegex(RuntimeError, "^TEST health response must be a JSON object$"):
            client.deployment()


if __name__ == "__main__":
    unittest.main()
