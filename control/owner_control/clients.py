from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable


JsonTransport = Callable[..., Any]


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 10,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        host = urllib.parse.urlsplit(url).netloc
        response_text = _http_error_text(error)
        remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
        if host.casefold() == "api.github.com" and (
            error.code == 429
            or remaining == "0"
            or "rate limit" in response_text.casefold()
        ):
            raise RuntimeError("GitHub API rate limit exhausted") from error
        raise RuntimeError(f"HTTP {error.code} from {host}") from error
    return value


class SymphonyClient:
    def __init__(self, base_url: str, *, transport: JsonTransport = request_json):
        self._url = base_url.rstrip("/") + "/api/v1/state"
        self._transport = transport

    def state(self) -> dict[str, Any]:
        value = self._transport("GET", self._url, timeout=5)
        if not isinstance(value, dict):
            raise RuntimeError("Symphony state response must be a JSON object")
        return value


class TestEnvironmentClient:
    def __init__(
        self,
        health_url: str,
        environment_url: str,
        *,
        transport: JsonTransport = request_json,
    ):
        self._health_url = health_url
        self._environment_url = environment_url
        self._transport = transport

    def deployment(self) -> dict[str, Any]:
        health = self._transport("GET", self._health_url, timeout=10)
        if not isinstance(health, dict):
            raise RuntimeError("TEST health response must be a JSON object")
        sha = health.get("build_sha") or health.get("git_sha") or health.get("sha")
        return {"sha": sha, "url": self._environment_url, "health_url": self._health_url}


class GitHubClient:
    _api_url = "https://api.github.com"
    _graphql_url = "https://api.github.com/graphql"

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        project_id: str,
        transport: JsonTransport = request_json,
        mutation_logger: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._repository = repository
        self._project_id = project_id
        self._transport = transport
        self._mutation_logger = mutation_logger or (lambda _entry: None)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "symphony-owner-control/1",
        }
        self._status_field_id: str | None = None
        self._status_options: dict[str, str] = {}
        self._project_items: dict[int, str] = {}
        self._project_statuses: dict[int, str] = {}
        self._lock = threading.RLock()

    def project_snapshot(self, *, reconcile_intake: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._project_snapshot(reconcile_intake=reconcile_intake)

    def _project_snapshot(self, *, reconcile_intake: bool) -> dict[str, Any]:
        items = []
        self._project_items = {}
        self._project_statuses = {}
        cursor = None
        while True:
            response = self._graphql(
                _PROJECT_QUERY,
                {"projectId": self._project_id, "cursor": cursor},
                operation_name="OwnerControlProject",
            )
            project = ((response.get("data") or {}).get("node") or {})
            self._remember_status_metadata(project.get("fields") or {})
            connection = project.get("items") or {}
            for node in connection.get("nodes") or []:
                item = self._normalize_project_item(node)
                if item is not None:
                    items.append(item)
                    self._project_items[item["number"]] = item["project_item_id"]
                    self._project_statuses[item["number"]] = str(item.get("status") or "")
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                raise RuntimeError("GitHub project pagination returned no cursor")
        project = {"updated_at": datetime.now(timezone.utc).isoformat(), "items": items}
        if reconcile_intake:
            self._reconcile_intake(project)
        return project

    def canonical(self, ref: str) -> dict[str, Any]:
        encoded_ref = urllib.parse.quote(ref, safe="")
        value = self._rest("GET", f"/repos/{self._repository}/git/ref/heads/{encoded_ref}")
        sha = (value.get("object") or {}).get("sha")
        check_runs = (
            self._rest("GET", f"/repos/{self._repository}/commits/{sha}/check-runs")
            if sha
            else {}
        )
        return {
            "sha": sha,
            "url": f"https://github.com/{self._repository}/commit/{sha}" if sha else None,
            "ref": ref,
            "ci": _canonical_ci(check_runs),
        }

    def commit_contains(self, deployed_sha: str, merge_sha: str) -> bool:
        if not deployed_sha or not merge_sha:
            return False
        deployed = urllib.parse.quote(deployed_sha, safe="")
        merged = urllib.parse.quote(merge_sha, safe="")
        comparison = self._rest(
            "GET",
            f"/repos/{self._repository}/compare/{merged}...{deployed}",
        )
        return str(comparison.get("status") or "").casefold() in {
            "ahead",
            "identical",
        }

    def set_status(self, issue: int, status: str) -> None:
        with self._lock:
            if issue not in self._project_items or not self._status_options:
                self.project_snapshot()
            item_id = self._project_items.get(issue)
            option_id = self._status_options.get(status.casefold())
            if not item_id:
                raise RuntimeError(f"issue #{issue} is not in the configured GitHub project")
            if not self._status_field_id or not option_id:
                raise RuntimeError(f"GitHub project status option is unavailable: {status}")
            old_status = self._project_statuses.get(issue)
            self._graphql(
                _STATUS_MUTATION,
                {
                    "projectId": self._project_id,
                    "itemId": item_id,
                    "fieldId": self._status_field_id,
                    "optionId": option_id,
                },
                operation_name="OwnerControlSetStatus",
            )
            self._project_statuses[issue] = status
            self._mutation_logger(
                {
                    "actor": "owner-control",
                    "issue": issue,
                    "old": old_status,
                    "new": status,
                    "reason": "set_status",
                }
            )

    def add_label(self, issue: int, label: str) -> None:
        self._rest_value(
            "POST",
            f"/repos/{self._repository}/issues/{issue}/labels",
            {"labels": [label]},
        )

    def remove_label(self, issue: int, label: str) -> None:
        encoded_label = urllib.parse.quote(label, safe="")
        self._transport(
            "DELETE",
            self._api_url + f"/repos/{self._repository}/issues/{issue}/labels/{encoded_label}",
            headers=self._headers,
            timeout=15,
        )

    def comment(self, issue: int, body: str) -> None:
        self._rest("POST", f"/repos/{self._repository}/issues/{issue}/comments", {"body": body})

    def comment_once(self, issue: int, body: str, action_key: str) -> None:
        digest = hashlib.sha256(action_key.encode("utf-8")).hexdigest()
        marker = f"<!-- owner-control-action:{digest} -->"
        page = 1
        while True:
            comments = self._rest_value(
                "GET",
                f"/repos/{self._repository}/issues/{issue}/comments?per_page=100&page={page}",
            )
            if not isinstance(comments, list):
                raise RuntimeError("GitHub issue comments response returned a non-list payload")
            if any(
                marker in str(comment.get("body") or "")
                for comment in comments
                if isinstance(comment, dict)
            ):
                return
            if len(comments) < 100:
                break
            page += 1
        self.comment(issue, f"{body}\n\n{marker}")

    def close_issue(self, issue: int) -> None:
        self._rest("PATCH", f"/repos/{self._repository}/issues/{issue}", {"state": "closed"})

    def _reconcile_intake(self, project: dict[str, Any]) -> None:
        items = project.get("items") or []
        newly_blocked: list[int] = []
        items_by_number = {
            int(item["number"]): item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("number"), int)
        }
        for issue in self._open_issues():
            if (
                issue.get("pull_request")
                or str(issue.get("state") or "").casefold() != "open"
            ):
                continue
            number = issue.get("number")
            node_id = issue.get("node_id")
            if not isinstance(number, int) or not node_id:
                raise RuntimeError("GitHub open issue is missing its number or node id")

            labels = _issue_labels(issue)
            owner_gated = _owner_gate_requested(issue, labels)
            current = items_by_number.get(number)

            if owner_gated:
                if _OWNER_WAITING_LABEL.casefold() not in {
                    label.casefold() for label in labels
                }:
                    self.add_label(number, _OWNER_WAITING_LABEL)
                    labels.append(_OWNER_WAITING_LABEL)
                if "symphony" in {label.casefold() for label in labels}:
                    self.remove_label(number, "symphony")
                    labels = [
                        label for label in labels if label.casefold() != "symphony"
                    ]
                was_missing = current is None
                current = current or self._add_project_issue(issue, labels, items, items_by_number)
                if str(current.get("status") or "").casefold() != "blocked":
                    self.set_status(number, "Blocked")
                _project_status(current, "Blocked")
                current["labels"] = labels
                if was_missing:
                    newly_blocked.append(number)
                continue

            if current is None:
                current = self._add_project_issue(issue, labels, items, items_by_number)
                self.set_status(number, "Ready for AI")
                _project_status(current, "Ready for AI")

        if newly_blocked:
            self._reassert_new_blocked(newly_blocked)

    def _reassert_new_blocked(self, issue_numbers: list[int]) -> None:
        time.sleep(_PROJECT_WORKFLOW_SETTLE_SECONDS)
        for issue_number in issue_numbers:
            if self._project_item_status(issue_number).casefold() != "blocked":
                self.set_status(issue_number, "Blocked")

        time.sleep(_PROJECT_WORKFLOW_CONFIRM_SECONDS)
        for issue_number in issue_numbers:
            if self._project_item_status(issue_number).casefold() == "blocked":
                continue
            self.set_status(issue_number, "Blocked")
            if self._project_item_status(issue_number).casefold() != "blocked":
                raise RuntimeError(
                    f"GitHub Project workflow did not retain Blocked for issue #{issue_number}"
                )

    def _project_item_status(self, issue_number: int) -> str:
        item_id = self._project_items.get(issue_number)
        if not item_id:
            raise RuntimeError(f"issue #{issue_number} is not in the configured GitHub project")
        response = self._graphql(
            _PROJECT_ITEM_STATUS_QUERY,
            {"itemId": item_id},
            operation_name="OwnerControlProjectItemStatus",
        )
        values = (((response.get("data") or {}).get("node") or {}).get("fieldValues") or {}).get("nodes") or []
        for value in values:
            if str((value.get("field") or {}).get("name") or "").casefold() == "status":
                return str(value.get("name") or "")
        return ""

    def _add_project_issue(
        self,
        issue: dict[str, Any],
        labels: list[str],
        items: list[dict[str, Any]],
        items_by_number: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        number = int(issue["number"])
        response = self._graphql(
            _ADD_PROJECT_ITEM_MUTATION,
            {"projectId": self._project_id, "contentId": issue["node_id"]},
            operation_name="OwnerControlAddProjectItem",
        )
        item_id = (
            ((response.get("data") or {}).get("addProjectV2ItemById") or {}).get("item")
            or {}
        ).get("id")
        if not item_id:
            raise RuntimeError(
                f"GitHub did not return the Project item for issue #{number}"
            )
        self._project_items[number] = item_id
        item = {
            "number": number,
            "identifier": f"#{number}",
            "title": issue.get("title"),
            "url": issue.get("html_url"),
            "status": "Backlog",
            "status_missing": True,
            "state": str(issue.get("state") or "OPEN").upper(),
            "labels": list(labels),
            "owner_question": None,
            "project_item_id": item_id,
            "pr": None,
            "ci": None,
        }
        items.append(item)
        items_by_number[number] = item
        return item

    def _open_issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            value = self._rest_value(
                "GET",
                f"/repos/{self._repository}/issues?state=open&per_page=100&page={page}",
            )
            if not isinstance(value, list):
                raise RuntimeError("GitHub open issues response returned a non-list payload")
            issues.extend(item for item in value if isinstance(item, dict))
            if len(value) < 100:
                return issues
            page += 1

    def _graphql(
        self, query: str, variables: dict[str, Any], *, operation_name: str
    ) -> dict[str, Any]:
        value = self._transport(
            "POST",
            self._graphql_url,
            headers=self._headers,
            body={"query": query, "variables": variables, "operationName": operation_name},
            timeout=15,
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub GraphQL response returned a non-object payload")
        errors = value.get("errors") or []
        if any(_is_graphql_rate_limit(error) for error in errors):
            raise RuntimeError("GitHub GraphQL rate limit exhausted")
        if errors:
            raise RuntimeError("GitHub GraphQL request failed")
        return value

    def _rest(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        value = self._rest_value(method, path, body)
        if not isinstance(value, dict):
            raise RuntimeError("GitHub REST response returned a non-object payload")
        return value

    def _rest_value(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._transport(
            method,
            self._api_url + path,
            headers=self._headers,
            body=body,
            timeout=15,
        )

    def _remember_status_metadata(self, fields: dict[str, Any]) -> None:
        for field in fields.get("nodes") or []:
            if str(field.get("name", "")).casefold() != "status":
                continue
            self._status_field_id = field.get("id")
            self._status_options = {
                str(option.get("name", "")).casefold(): option.get("id")
                for option in field.get("options") or []
                if option.get("id") and option.get("name")
            }

    @staticmethod
    def _normalize_project_item(node: dict[str, Any]) -> dict[str, Any] | None:
        content = node.get("content") or {}
        if content.get("__typename") != "Issue" or content.get("number") is None:
            return None
        status = None
        for value in (node.get("fieldValues") or {}).get("nodes") or []:
            if str((value.get("field") or {}).get("name", "")).casefold() == "status":
                status = value.get("name")
                break
        pull_requests = (content.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        pull_request = next((pr for pr in pull_requests if pr.get("merged")), None)
        pull_request = pull_request or (pull_requests[0] if pull_requests else None)
        normalized_pr, ci = _normalize_pull_request(pull_request)
        comments = (content.get("comments") or {}).get("nodes") or []
        return {
            "number": content["number"],
            "identifier": f"#{content['number']}",
            "title": content.get("title"),
            "url": content.get("url"),
            "status": status or "Backlog",
            "status_missing": status is None,
            "state": content.get("state"),
            "labels": [label.get("name") for label in (content.get("labels") or {}).get("nodes") or [] if label.get("name")],
            "owner_question": extract_owner_question(comments),
            "project_item_id": node.get("id"),
            "pr": normalized_pr,
            "ci": ci,
        }


_OWNER_WAITING_LABEL = "ждёт-владельца"
_PROJECT_WORKFLOW_SETTLE_SECONDS = 1.0
_PROJECT_WORKFLOW_CONFIRM_SECONDS = 0.5
_OWNER_GATE_FIELD = re.compile(
    r"(?ims)^###\s*Пригодность\s*\r?\n+(?P<answer>.*?)(?=^###\s|\Z)"
)


def _issue_labels(issue: dict[str, Any]) -> list[str]:
    return [
        str(label.get("name"))
        for label in issue.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]


def _owner_gate_requested(issue: dict[str, Any], labels: list[str]) -> bool:
    if _OWNER_WAITING_LABEL.casefold() in {label.casefold() for label in labels}:
        return True
    field = _OWNER_GATE_FIELD.search(str(issue.get("body") or ""))
    if field is None:
        return False
    return bool(
        re.fullmatch(
            r"красная\s*[—-]\s*решает\s+человек",
            field.group("answer").strip(),
            re.IGNORECASE,
        )
    )


def _project_status(item: dict[str, Any], status: str) -> None:
    item["status"] = status
    item["status_missing"] = False


def _http_error_text(error: urllib.error.HTTPError) -> str:
    try:
        return error.read(4096).decode("utf-8", errors="replace")
    except (AttributeError, OSError):
        return ""


def _is_graphql_rate_limit(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    error_type = str(error.get("type") or "").casefold()
    error_code = str(error.get("code") or "").casefold()
    message = str(error.get("message") or "").casefold()
    return (
        "rate_limit" in error_type
        or "rate_limit" in error_code
        or "rate limit" in message
    )


def extract_owner_question(comments: list[dict[str, Any]]) -> str | None:
    marker = re.compile(
        r"(?:owner\s+question|вопрос\s+владельцу|нужен\s+выбор\s+владельца|нужно\s+решение\s+владельца)\s*:\s*(.+)",
        re.IGNORECASE | re.DOTALL,
    )
    for comment in reversed(comments or []):
        match = marker.search(str(comment.get("body") or ""))
        if match:
            return match.group(1).strip()[:2000]
    return None


def _normalize_pull_request(pull_request: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not pull_request:
        return None, None
    commits = (pull_request.get("commits") or {}).get("nodes") or []
    commit = (commits[-1].get("commit") or {}) if commits else {}
    rollup = commit.get("statusCheckRollup") or {}
    state = str(rollup.get("state") or "unknown").casefold()
    pr = {
        "number": pull_request.get("number"),
        "url": pull_request.get("url"),
        "state": pull_request.get("state"),
        "merged": bool(pull_request.get("merged")),
        "sha": commit.get("oid"),
        "merge_sha": (pull_request.get("mergeCommit") or {}).get("oid"),
    }
    ci = {"status": state, "url": f"{pull_request.get('url')}/checks" if pull_request.get("url") else None}
    return pr, ci


def _canonical_ci(value: dict[str, Any]) -> dict[str, Any]:
    runs = value.get("check_runs") or [] if isinstance(value, dict) else []
    latest_by_name: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for run in runs:
        name = str(run.get("name") or "").strip().casefold()
        if not name:
            unnamed.append(run)
            continue
        previous = latest_by_name.get(name)
        run_id = run.get("id")
        previous_id = previous.get("id") if previous else None
        if previous is None or (
            isinstance(run_id, int)
            and (not isinstance(previous_id, int) or run_id > previous_id)
        ):
            latest_by_name[name] = run
    runs = unnamed + list(latest_by_name.values())
    non_canonical_checks = {
        "assign",
        "exact-sha test deploy и smoke",
        "land",
        "queue-dispatch",
        "reconcile",
        "запустить exact-sha test после dispatched canonical ci",
    }
    runs = [
        run
        for run in runs
        if str(run.get("name") or "").casefold() not in non_canonical_checks
    ]
    failed_conclusions = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    failed = sum(
        1
        for run in runs
        if str(run.get("conclusion") or "").casefold() in failed_conclusions
    )
    pending = sum(
        1 for run in runs if str(run.get("status") or "").casefold() != "completed"
    )
    total = len(runs)
    if failed:
        status = "failure"
    elif pending:
        status = "pending"
    elif total:
        status = "success"
    else:
        status = "unknown"
    return {"status": status, "total": total, "failed": failed, "pending": pending}


_PROJECT_QUERY = """
query OwnerControlProject($projectId: ID!, $cursor: String) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
      }
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              number title url state
              labels(first: 30) { nodes { name } }
              comments(last: 20) { nodes { body } }
              closedByPullRequestsReferences(first: 10) {
                nodes {
                  number url state merged
                  mergeCommit { oid }
                  commits(last: 1) {
                    nodes { commit { oid statusCheckRollup { state } } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
_STATUS_MUTATION = """
mutation OwnerControlSetStatus($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) { projectV2Item { id } }
}
"""

_ADD_PROJECT_ITEM_MUTATION = """
mutation OwnerControlAddProjectItem($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""

_PROJECT_ITEM_STATUS_QUERY = """
query OwnerControlProjectItemStatus($itemId: ID!) {
  node(id: $itemId) {
    ... on ProjectV2Item {
      fieldValues(first: 20) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            field { ... on ProjectV2FieldCommon { name } }
          }
        }
      }
    }
  }
}
"""
