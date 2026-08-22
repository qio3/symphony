from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable


JsonTransport = Callable[..., dict[str, Any]]


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
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
        raise RuntimeError(f"HTTP {error.code} from {urllib.parse.urlsplit(url).netloc}") from error
    if not isinstance(value, dict):
        raise RuntimeError("JSON endpoint returned a non-object response")
    return value


class SymphonyClient:
    def __init__(self, base_url: str, *, transport: JsonTransport = request_json):
        self._url = base_url.rstrip("/") + "/api/v1/state"
        self._transport = transport

    def state(self) -> dict[str, Any]:
        return self._transport("GET", self._url, timeout=5)


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
    ):
        self._repository = repository
        self._project_id = project_id
        self._transport = transport
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "symphony-owner-control/1",
        }
        self._status_field_id: str | None = None
        self._status_options: dict[str, str] = {}
        self._project_items: dict[int, str] = {}

    def project_snapshot(self) -> dict[str, Any]:
        items = []
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
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                raise RuntimeError("GitHub project pagination returned no cursor")
        return {"updated_at": datetime.now(timezone.utc).isoformat(), "items": items}

    def canonical(self, ref: str) -> dict[str, Any]:
        encoded_ref = urllib.parse.quote(ref, safe="")
        value = self._rest("GET", f"/repos/{self._repository}/git/ref/heads/{encoded_ref}")
        sha = (value.get("object") or {}).get("sha")
        return {
            "sha": sha,
            "url": f"https://github.com/{self._repository}/commit/{sha}" if sha else None,
            "ref": ref,
        }

    def set_status(self, issue: int, status: str) -> None:
        if issue not in self._project_items or not self._status_options:
            self.project_snapshot()
        item_id = self._project_items.get(issue)
        option_id = self._status_options.get(status.casefold())
        if not item_id:
            raise RuntimeError(f"issue #{issue} is not in the configured GitHub project")
        if not self._status_field_id or not option_id:
            raise RuntimeError(f"GitHub project status option is unavailable: {status}")
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

    def add_label(self, issue: int, label: str) -> None:
        self._rest("POST", f"/repos/{self._repository}/issues/{issue}/labels", {"labels": [label]})

    def comment(self, issue: int, body: str) -> None:
        self._rest("POST", f"/repos/{self._repository}/issues/{issue}/comments", {"body": body})

    def close_issue(self, issue: int) -> None:
        self._rest("PATCH", f"/repos/{self._repository}/issues/{issue}", {"state": "closed"})

    def _graphql(self, query: str, variables: dict[str, Any], *, operation_name: str) -> dict[str, Any]:
        value = self._transport(
            "POST",
            self._graphql_url,
            headers=self._headers,
            body={"query": query, "variables": variables, "operationName": operation_name},
            timeout=15,
        )
        if value.get("errors"):
            raise RuntimeError("GitHub GraphQL request failed")
        return value

    def _rest(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "state": content.get("state"),
            "labels": [label.get("name") for label in (content.get("labels") or {}).get("nodes") or [] if label.get("name")],
            "owner_question": extract_owner_question(comments),
            "project_item_id": node.get("id"),
            "pr": normalized_pr,
            "ci": ci,
        }


def extract_owner_question(comments: list[dict[str, Any]]) -> str | None:
    marker = re.compile(r"(?:owner\s+question|вопрос\s+владельцу)\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
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
    }
    ci = {"status": state, "url": f"{pull_request.get('url')}/checks" if pull_request.get("url") else None}
    return pr, ci


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
