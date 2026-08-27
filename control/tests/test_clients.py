import io
import threading
import unittest
import urllib.error
from unittest.mock import patch

from owner_control.clients import (
    GitHubClient,
    SymphonyClient,
    TestEnvironmentClient,
    _owner_gate_requested,
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
                                {"id": "blocked", "name": "Blocked"},
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
                                            "mergeCommit": {"oid": "merge999"},
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
    def test_canonical_includes_deterministic_check_run_health(self):
        transport = FakeTransport([
            {"object": {"sha": "canonical-sha"}},
            {
                "total_count": 2,
                "check_runs": [
                    {"status": "completed", "conclusion": "success", "html_url": "https://ci/1"},
                    {"status": "completed", "conclusion": "failure", "html_url": "https://ci/2"},
                ],
            },
        ])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        canonical = client.canonical("main")

        self.assertEqual(canonical["sha"], "canonical-sha")
        self.assertEqual(canonical["ci"]["status"], "failure")
        self.assertEqual(canonical["ci"]["failed"], 1)

    def test_canonical_ignores_landing_valve_checks_when_evaluating_code_health(self):
        transport = FakeTransport([
            {"object": {"sha": "canonical-sha"}},
            {
                "total_count": 3,
                "check_runs": [
                    {
                        "name": "queue-dispatch",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "name": "land",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                    {
                        "name": "pytest shard a (2 workers)",
                        "status": "in_progress",
                        "conclusion": None,
                    },
                ],
            },
        ])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        canonical = client.canonical("main")

        self.assertEqual(canonical["ci"]["status"], "pending")
        self.assertEqual(canonical["ci"]["total"], 1)
        self.assertEqual(canonical["ci"]["failed"], 0)
        self.assertEqual(canonical["ci"]["pending"], 1)

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
                "status_missing": False,
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
                    "merge_sha": "merge999",
                },
                "ci": {"status": "success", "url": "https://github.test/pull/99/checks"},
            },
        )

    def test_commit_containment_uses_github_compare(self):
        transport = FakeTransport([{"status": "ahead", "ahead_by": 4}])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        self.assertTrue(client.commit_contains("deployed-sha", "merge-sha"))
        self.assertIn(
            "/repos/qio3/zavod/compare/merge-sha...deployed-sha",
            transport.calls[0][1],
        )

    def test_comment_once_reuses_deterministic_marker_after_ambiguous_response(self):
        action_key = '["rework", {"issue": 401, "reason": "fix"}]'
        transport = FakeTransport([[], {"id": 10}])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        client.comment_once(401, "Owner requested rework: fix", action_key)
        posted_body = transport.calls[1][2]["body"]
        self.assertIn("<!-- owner-control-action:", posted_body)

        retry_transport = FakeTransport([[{"id": 10, "body": posted_body}]])
        retry_client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=retry_transport,
        )
        retry_client.comment_once(401, "Owner requested rework: fix", action_key)

        self.assertEqual(len(retry_transport.calls), 1)

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

    def test_set_status_records_actor_old_new_and_reason(self):
        journal = []
        transport = FakeTransport([
            project_response(),
            {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-401"}}}},
        ])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
            mutation_logger=journal.append,
        )
        client.project_snapshot()

        client.set_status(401, "Done")

        self.assertEqual(journal[0]["actor"], "owner-control")
        self.assertEqual(journal[0]["issue"], 401)
        self.assertEqual(journal[0]["old"], "Ready for Acceptance")
        self.assertEqual(journal[0]["new"], "Done")
        self.assertEqual(journal[0]["reason"], "set_status")

    def test_reconciled_snapshot_adds_every_missing_open_issue_with_a_deterministic_status(self):
        ordinary = {
            "node_id": "issue-node-402",
            "number": 402,
            "title": "Ordinary work",
            "html_url": "https://github.test/issues/402",
            "state": "open",
            "labels": [{"name": "backend"}],
            "body": "### Пригодность\n\nзелёная — берёт сессия",
        }
        owner_gated = {
            "node_id": "issue-node-403",
            "number": 403,
            "title": "Needs owner",
            "html_url": "https://github.test/issues/403",
            "state": "open",
            "labels": [{"name": "ждёт-владельца"}],
            "body": "Owner must choose a vendor.",
        }
        transport = FakeTransport(
            [
                project_response(),
                [ordinary, owner_gated],
                {"data": {"addProjectV2ItemById": {"item": {"id": "project-item-402"}}}},
                {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-402"}}}},
                {"data": {"addProjectV2ItemById": {"item": {"id": "project-item-403"}}}},
                {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-403"}}}},
                {
                    "data": {
                        "node": {
                            "fieldValues": {
                                "nodes": [
                                    {"name": "Ready for AI", "field": {"name": "Status"}}
                                ]
                            }
                        }
                    }
                },
                {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-403"}}}},
                {
                    "data": {
                        "node": {
                            "fieldValues": {
                                "nodes": [{"name": "Blocked", "field": {"name": "Status"}}]
                            }
                        }
                    }
                },
            ]
        )
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        with patch("owner_control.clients.time.sleep"):
            project = client.project_snapshot(reconcile_intake=True)

        added = {item["number"]: item for item in project["items"] if item["number"] in {402, 403}}
        self.assertEqual(added[402]["status"], "Ready for AI")
        self.assertEqual(added[403]["status"], "Blocked")
        self.assertFalse(added[402]["status_missing"])
        self.assertFalse(added[403]["status_missing"])
        graphql_operations = [
            call[2]["operationName"]
            for call in transport.calls
            if call[1] == "https://api.github.com/graphql"
        ]
        self.assertEqual(
            graphql_operations,
            [
                "OwnerControlProject",
                "OwnerControlAddProjectItem",
                "OwnerControlSetStatus",
                "OwnerControlAddProjectItem",
                "OwnerControlSetStatus",
                "OwnerControlProjectItemStatus",
                "OwnerControlSetStatus",
                "OwnerControlProjectItemStatus",
            ],
        )

    def test_reconciled_snapshot_blocks_red_form_and_removes_symphony_lease(self):
        response = project_response()
        item = response["data"]["node"]["items"]["nodes"][0]
        item["fieldValues"]["nodes"][0]["name"] = "Ready for AI"
        item["content"]["labels"]["nodes"] = [{"name": "symphony"}]
        red_form_issue = {
            "node_id": "issue-node-401",
            "number": 401,
            "title": "Needs a live owner decision",
            "html_url": "https://github.test/issues/401",
            "state": "open",
            "labels": [{"name": "symphony"}],
            "body": "### Пригодность\n\nкрасная — решает человек",
        }
        transport = FakeTransport(
            [
                response,
                [red_form_issue],
                [],
                [],
                {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "project-item-401"}}}},
            ]
        )
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        project = client.project_snapshot(reconcile_intake=True)

        issue = project["items"][0]
        self.assertEqual(issue["status"], "Blocked")
        self.assertIn("ждёт-владельца", issue["labels"])
        self.assertNotIn("symphony", issue["labels"])
        self.assertEqual(
            [(call[0], call[1]) for call in transport.calls[2:]],
            [
                ("POST", "https://api.github.com/repos/qio3/zavod/issues/401/labels"),
                ("DELETE", "https://api.github.com/repos/qio3/zavod/issues/401/labels/symphony"),
                ("POST", "https://api.github.com/graphql"),
            ],
        )

    def test_reconciled_snapshot_never_auto_unblocks_an_existing_blocked_issue(self):
        response = project_response()
        item = response["data"]["node"]["items"]["nodes"][0]
        item["fieldValues"]["nodes"][0]["name"] = "Blocked"
        item["content"]["labels"]["nodes"] = []
        ordinary = {
            "node_id": "issue-node-401",
            "number": 401,
            "title": "Still intentionally blocked",
            "html_url": "https://github.test/issues/401",
            "state": "open",
            "labels": [],
            "body": "No automatic owner gate marker remains.",
        }
        transport = FakeTransport([response, [ordinary]])
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        project = client.project_snapshot(reconcile_intake=True)

        self.assertEqual(project["items"][0]["status"], "Blocked")
        self.assertEqual(len(transport.calls), 2)

    def test_reconciled_snapshot_leaves_an_existing_blank_status_unchanged(self):
        response = project_response()
        item = response["data"]["node"]["items"]["nodes"][0]
        item["fieldValues"]["nodes"] = []
        ordinary = {
            "node_id": "issue-node-401",
            "number": 401,
            "title": "Missing status",
            "html_url": "https://github.test/issues/401",
            "state": "open",
            "labels": [],
            "body": "Regular issue.",
        }
        transport = FakeTransport(
            [
                response,
                [ordinary],
            ]
        )
        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )

        project = client.project_snapshot(reconcile_intake=True)

        self.assertEqual(project["items"][0]["status"], "Backlog")
        self.assertTrue(project["items"][0]["status_missing"])
        self.assertEqual(len(transport.calls), 2)

    def test_red_owner_gate_text_is_valid_only_inside_the_exact_issue_form_field(self):
        quoted_elsewhere = (
            "### Что не так\n\nСправка перечисляет варианты:\n"
            "красная — решает человек\n\n### Как проверить\n\nТекст виден."
        )

        self.assertFalse(_owner_gate_requested({"body": quoted_elsewhere}, []))

    def test_project_reconciliation_serializes_status_actions_behind_metadata_refresh(self):
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        action_finished = threading.Event()
        calls = []

        def transport(method, url, *, headers=None, body=None, timeout=10):
            calls.append((method, url, body))
            if url.endswith("/graphql") and body["operationName"] == "OwnerControlProject":
                return project_response()
            if "/issues?" in url:
                refresh_started.set()
                release_refresh.wait(timeout=2)
                return []
            return {
                "data": {
                    "updateProjectV2ItemFieldValue": {
                        "projectV2Item": {"id": "project-item-401"}
                    }
                }
            }

        client = GitHubClient(
            token="token",
            repository="qio3/zavod",
            project_id="project-id",
            transport=transport,
        )
        refresh = threading.Thread(
            target=lambda: client.project_snapshot(reconcile_intake=True)
        )
        refresh.start()
        self.assertTrue(refresh_started.wait(timeout=1))

        action = threading.Thread(
            target=lambda: (client.set_status(401, "Done"), action_finished.set())
        )
        action.start()

        self.assertFalse(action_finished.wait(timeout=0.05))
        release_refresh.set()
        refresh.join(timeout=2)
        action.join(timeout=2)
        self.assertTrue(action_finished.is_set())

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
