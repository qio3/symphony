"use strict";

const THEME_STORAGE_KEY = "owner-control-theme";
const TABLE_PAGE_SIZE = 12;
const csrf = document.querySelector('meta[name="owner-control-csrf"]').content;
const runtimeUrl = document.querySelector('meta[name="runtime-diagnostics-url"]').content;
const app = document.getElementById("app");
const snapshotStatus = document.getElementById("snapshot-status");
const globalNotice = document.getElementById("global-notice");
const refreshButton = document.getElementById("refresh-button");
const runtimeLink = document.getElementById("runtime-link");
const actionDialog = document.getElementById("action-dialog");
const dialogForm = document.getElementById("dialog-form");
const dialogTitle = document.getElementById("dialog-title");
const dialogBody = document.getElementById("dialog-body");
const dialogConfirm = document.getElementById("dialog-confirm");
const toastRegion = document.getElementById("toast-region");
const themeToggle = document.getElementById("theme-toggle");
const ownerTabs = document.getElementById("owner-tabs");
const issueTablePanel = document.getElementById("issue-table-panel");
const issueTableBody = document.getElementById("issue-table-body");
const issueDrawer = document.getElementById("issue-drawer");
const issueDrawerContent = document.getElementById("issue-drawer-content");
const issueDrawerTitle = document.getElementById("issue-drawer-title");
const issueDrawerKicker = document.getElementById("issue-drawer-kicker");
const showMoreButton = document.getElementById("issue-show-more");
const workbenchBody = document.querySelector(".workbench-body");

runtimeLink.href = runtimeUrl;

let currentSnapshot = null;
let requestInFlight = false;
let actionInFlight = false;
let pendingAction = null;
let renderedSignature = null;
let activeTab = "blocked";
let activeTabInitialized = false;
let selectedIssue = null;
let drawerDismissed = false;
let visibleRows = TABLE_PAGE_SIZE;
let workChart = null;
let workChartSignature = null;
const actionErrors = new Map();

const targets = {
  serviceFacts: document.getElementById("service-facts"),
  serviceActions: document.getElementById("service-actions"),
  delivery: document.getElementById("delivery-content"),
  quota: document.getElementById("quota-content"),
  sourceHealth: document.getElementById("source-health"),
  diagnosticWork: document.getElementById("diagnostic-work"),
  logs: document.getElementById("diagnostic-logs"),
};

function browserHeaders(json = false) {
  const headers = { "X-Owner-Control-CSRF": csrf };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

function setTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  const dark = theme === "dark";
  themeToggle.setAttribute("aria-pressed", String(dark));
  themeToggle.setAttribute("aria-label", dark ? "Use light theme" : "Use dark theme");
  themeToggle.textContent = dark ? "☼" : "◐";
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, theme); } catch (_error) { /* unavailable storage */ }
  }
  if (currentSnapshot) renderWorkChart(currentSnapshot);
}

function initialTheme() {
  const preloaded = document.documentElement.dataset.theme;
  if (preloaded === "light" || preloaded === "dark") return preloaded;
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch (_error) { /* unavailable storage */ }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

async function refreshSnapshot(options = {}) {
  if (requestInFlight) return false;
  requestInFlight = true;
  refreshButton.disabled = true;
  if (currentSnapshot && options.announce !== false) snapshotStatus.textContent = "Refreshing…";
  try {
    const response = await fetch("/ui/snapshot", { headers: browserHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error(`Snapshot unavailable (${response.status})`);
    currentSnapshot = await response.json();
    render(currentSnapshot);
    return true;
  } catch (error) {
    showStaleError(error.message);
    if (!currentSnapshot) renderUnavailable(error.message);
    return false;
  } finally {
    requestInFlight = false;
    refreshButton.disabled = false;
    app.setAttribute("aria-busy", "false");
  }
}

function render(snapshot, options = {}) {
  const signature = snapshotSignature(snapshot);
  renderFreshness(snapshot);
  if (!options.force && signature === renderedSignature) return;
  const viewState = captureViewState();
  renderService(snapshot);
  targets.delivery.classList.remove("loading-block");
  targets.delivery.removeAttribute("role");
  targets.delivery.removeAttribute("aria-label");
  targets.quota.classList.remove("loading-block");
  targets.quota.removeAttribute("role");
  targets.quota.removeAttribute("aria-label");
  renderDelivery(snapshot);
  renderQuota(snapshot);
  renderWorkbench(snapshot);
  renderSources(snapshot);
  renderWorkDiagnostics(snapshot);
  renderActionErrors();
  app.classList.toggle("action-busy", actionInFlight);
  restoreViewState(viewState);
  renderedSignature = signature;
}

function renderFreshness(snapshot) {
  const staleSources = Object.entries(snapshot.sources || {}).filter(([, value]) => value?.status !== "fresh");
  const stale = Boolean(snapshot.stale || staleSources.length);
  const refreshedAt = snapshot.refreshed_at || snapshot.generated_at;
  snapshotStatus.textContent = stale ? `Stale · ${relativeTime(refreshedAt)}` : `Updated ${relativeTime(refreshedAt)}`;
  snapshotStatus.className = `status-chip ${stale ? "is-stale" : "is-live"}`;
  if (stale) {
    const names = staleSources.map(([name]) => sourceName(name)).join(", ");
    const githubRateLimited = staleSources.some(([name, source]) => (
      name === "github" && /rate limit exhausted/i.test(String(source?.error || ""))
    ));
    globalNotice.textContent = githubRateLimited
      ? "GitHub request quota is exhausted. Last confirmed data remains visible; retrying automatically."
      : `Last confirmed data remains visible. Waiting for ${names || "a source"}.`;
    globalNotice.className = "notice is-stale-notice";
  } else {
    globalNotice.className = "notice is-hidden";
    globalNotice.textContent = "";
  }
}

function renderService(snapshot) {
  const service = snapshot.service || {};
  const runtimeSource = snapshot.sources?.runtime || {};
  const serviceStatus = String(service.status || "unknown").toLowerCase();
  const serviceKnown = serviceStatus !== "unknown";
  const isRunning = service.live === true;
  const isTransitioning = ["created", "restarting", "starting", "stopping"].includes(serviceStatus);
  const transitionLabel = serviceStatus === "stopping" ? "Stopping…" : "Starting…";
  const serviceTitle = document.getElementById("service-title");
  serviceTitle.textContent = isTransitioning ? transitionLabel : serviceKnown ? (isRunning ? "Running" : "Stopped") : "Status unavailable";
  serviceTitle.className = isTransitioning ? "service-starting" : isRunning ? "service-running" : "service-stopped";

  clear(targets.serviceFacts);
  targets.serviceFacts.append(
    fact("Service", isTransitioning ? transitionLabel : serviceKnown ? (isRunning ? "Running" : "Stopped") : "Unknown", isTransitioning ? "warning" : isRunning ? "good" : "danger"),
    fact("Intake", snapshot.intake?.active ? "Active" : "Paused", snapshot.intake?.active ? "good" : "warning"),
    fact("Workers", `${number(snapshot.workers?.running)}/${number(snapshot.workers?.limit)}`, "neutral"),
    fact("Runtime API", titleCase(runtimeSource.status || "unknown"), runtimeSource.status === "fresh" ? "good" : "warning"),
  );

  clear(targets.serviceActions);
  const supervisorFresh = snapshot.sources?.supervisor?.status === "fresh";
  const runtimeFresh = snapshot.sources?.runtime?.status === "fresh";
  const githubFresh = snapshot.sources?.github?.status === "fresh";
  if (isTransitioning) {
    const pending = el("button", "button secondary", transitionLabel);
    pending.type = "button";
    pending.disabled = true;
    pending.title = "Waiting for the fixed Symphony service state to be confirmed.";
    targets.serviceActions.append(pending);
  } else if (isRunning) {
    const intakeActive = snapshot.intake?.active === true;
    const intake = actionButton(
      intakeActive ? "Pause intake" : "Resume intake",
      intakeActive ? "pause" : "resume",
      "primary",
      !intakeActive && (!runtimeFresh || !githubFresh),
    );
    if (intake.disabled) intake.title = "Resume requires fresh runtime and GitHub state.";
    const advanced = el("details", "service-advanced");
    advanced.append(el("summary", "button secondary", "Service actions"));
    const advancedActions = el("div", "service-advanced-actions");
    const restart = actionButton("Restart", "restart", "secondary", !supervisorFresh);
    const stop = actionButton("Stop Symphony", "stop_service", "danger", !supervisorFresh || !runtimeFresh);
    if (!supervisorFresh) restart.title = "Waiting for fresh supervisor state.";
    if (!supervisorFresh || !runtimeFresh) stop.title = "Waiting for fresh supervisor and runtime state.";
    advancedActions.append(restart, stop);
    advanced.append(advancedActions);
    targets.serviceActions.append(intake, advanced);
  } else if (serviceKnown) {
    const start = actionButton("Start Symphony", "start_service", "primary", !supervisorFresh);
    if (!supervisorFresh) start.title = "Waiting for fresh supervisor state.";
    targets.serviceActions.append(start);
  }
}

function renderDelivery(snapshot) {
  clear(targets.delivery);
  const canonical = snapshot.canonical || {};
  const test = snapshot.test || {};
  const delivery = el("div", "delivery-stack");
  delivery.append(
    deliveryRow("Canonical", canonical.sha, canonical.url, "Source of truth"),
    deliveryRow("TEST", test.sha, test.url, test.synced ? "Synced" : test.drift ? "Drift" : "Unknown", test.synced ? "good" : "warning"),
  );
  if (test.drift) {
    const warning = el("div", "delivery-warning");
    warning.textContent = "TEST is not on the canonical SHA. Acceptance is disabled.";
    delivery.append(warning);
  }
  targets.delivery.append(delivery);
}

function renderQuota(snapshot) {
  clear(targets.quota);
  const quota = snapshot.quota || {};
  targets.quota.append(quotaWindow("5 hour", quota.five_hour), quotaWindow("Weekly", quota.weekly));
}

function renderWorkbench(snapshot) {
  const tabs = workbenchTabs(snapshot);
  if (!activeTabInitialized) {
    activeTab = tabs.find(([key]) => workItems(snapshot, key).length)?.[0] || "queue";
    activeTabInitialized = true;
  }
  clear(ownerTabs);
  for (const [key, label, count, tone] of tabs) {
    const tab = el("button", "owner-tab " + tone, label);
    const selected = activeTab === key;
    tab.type = "button";
    tab.id = `owner-tab-${key}`;
    tab.dataset.ownerTab = key;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "issue-table-panel");
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    tab.append(el("span", "tab-count", count));
    ownerTabs.append(tab);
  }
  issueTablePanel.setAttribute("aria-labelledby", `owner-tab-${activeTab}`);
  const items = workItems(snapshot, activeTab);
  if (!items.some((item) => String(item.number) === String(selectedIssue))) {
    selectedIssue = null;
    if (!drawerDismissed && !narrowWorkbench()) selectedIssue = items[0]?.number ?? null;
  }
  clear(issueTableBody);
  for (const item of items.slice(0, visibleRows)) issueTableBody.append(issueTableRow(item));
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = el("td", "empty-table-cell", "No Issues in this queue.");
    cell.colSpan = 4;
    row.append(cell);
    issueTableBody.append(row);
  }
  const remaining = items.length - Math.min(items.length, visibleRows);
  showMoreButton.classList.toggle("is-hidden", remaining <= 0);
  showMoreButton.textContent = "Show " + Math.min(remaining, TABLE_PAGE_SIZE) + " more";
  renderDrawer(findWorkbenchItem(snapshot, selectedIssue));
  renderWorkChart(snapshot);
}

function workbenchTabs(snapshot) {
  return [
    ["blocked", "Needs owner", number(snapshot.counts?.blocked) + number(snapshot.counts?.quarantined), "danger"],
    ["running", "Running", number(snapshot.counts?.running), "neutral"],
    ["ready", "Ready", number(snapshot.counts?.ready_for_acceptance), "good"],
    ["queue", "Queue", number(snapshot.counts?.ready_for_ai) + number(snapshot.counts?.backlog), "neutral"],
  ];
}

function workItems(snapshot, tab) {
  const owner = snapshot.owner_view || {};
  const lanes = {
    blocked: (owner.blocked || []).map((item) => ({ ...item, lane: "blocked" })).concat((owner.system_quarantines || []).map((item) => ({ ...item, lane: "quarantine" }))),
    running: (owner.work_items || []).map((item) => ({ ...item, lane: "running" })),
    ready: (owner.ready_for_acceptance || []).map((item) => ({ ...item, lane: "ready" })),
    queue: (owner.backlog || []).map((item) => ({ ...item, lane: "queue" })),
  };
  const unique = new Map();
  for (const item of lanes[tab] || []) if (!unique.has(String(item.number))) unique.set(String(item.number), item);
  return [...unique.values()];
}

function findWorkbenchItem(snapshot, issue) {
  if (issue === null || issue === undefined) return null;
  for (const tab of ["blocked", "running", "ready", "queue"]) {
    const found = workItems(snapshot, tab).find((item) => String(item.number) === String(issue));
    if (found) return found;
  }
  return null;
}

function issueTableRow(item) {
  const row = document.createElement("tr");
  row.setAttribute("data-issue-row", String(item.number || ""));
  row.dataset.issueCard = String(item.number || "");
  row.setAttribute("aria-selected", String(String(selectedIssue) === String(item.number)));
  const issue = el("button", "issue-select");
  issue.type = "button";
  issue.dataset.issueSelect = String(item.number || "");
  issue.setAttribute("aria-expanded", String(String(selectedIssue) === String(item.number)));
  issue.append(el("strong", "", "#" + item.number), el("span", "issue-title", item.title || "Untitled Issue"));
  const issueCell = document.createElement("td");
  issueCell.dataset.label = "Issue";
  issueCell.append(issue);
  const stateCell = el("td", "state-cell " + item.lane, laneLabel(item));
  stateCell.dataset.label = "State";

  const progressCell = document.createElement("td");
  progressCell.dataset.label = "Progress";
  const progress = item.lane === "blocked" || item.lane === "quarantine"
    ? item.question || item.reason || "Owner input required"
    : item.display_phase || item.stage || item.status || "—";
  progressCell.append(el("strong", "row-phase", progress));
  if (item.lane === "running") {
    const facts = el("div", "cell-meta");
    facts.append(el("span", "", modelName(item.model)));
    const elapsedValue = el("span", "", elapsed(item.started_at));
    if (item.started_at) elapsedValue.dataset.startedAt = item.started_at;
    facts.append(elapsedValue, el("span", "", `${number(item.turn_count)} turns`));
    progressCell.append(facts);
  }

  const deliveryCell = document.createElement("td");
  deliveryCell.dataset.label = "Delivery / usage";
  const evidence = el("div", "evidence-summary");
  if (item.pr || item.ci || item.test) {
    evidence.append(
      evidenceLink(item.pr?.url, item.pr?.number ? `PR #${item.pr.number}` : "PR —", item.pr?.url ? "neutral" : "muted"),
      evidenceLink(item.ci?.url, `CI ${titleCase(item.ci?.status || "unknown")}`, statusTone(item.ci?.status)),
      evidenceLink(item.test?.url, `TEST ${titleCase(item.test?.status || (item.test?.synced ? "synced" : "unknown"))}`, item.test?.synced ? "good" : statusTone(item.test?.status)),
    );
  } else {
    evidence.append(el("span", "muted", "—"));
  }
  deliveryCell.append(evidence);
  if (item.lane === "running" || item.lane === "ready") {
    const tokens = item.usage?.total_tokens;
    const usage = el("div", "usage-summary", Number.isFinite(Number(tokens)) ? `${formatNumber(tokens)} tokens · ${estimatedCredits(item.usage?.estimated_credits_micros)}` : "Task usage unavailable");
    deliveryCell.append(usage);
  }
  row.append(issueCell, stateCell, progressCell, deliveryCell);
  return row;
}

function selectWorkbenchIssue(issue) {
  selectedIssue = issue;
  drawerDismissed = false;
  for (const row of document.querySelectorAll("[data-issue-row]")) {
    const selected = String(row.dataset.issueRow) === String(issue);
    row.setAttribute("aria-selected", String(selected));
    row.querySelector("[data-issue-select]")?.setAttribute("aria-expanded", String(selected));
  }
  renderDrawer(findWorkbenchItem(currentSnapshot || {}, issue));
}

function renderDrawer(item) {
  clear(issueDrawerContent);
  if (!item) {
    issueDrawer.classList.remove("is-open");
    issueDrawer.setAttribute("aria-hidden", "true");
    issueDrawer.inert = true;
    delete issueDrawer.dataset.issueCard;
    workbenchBody.classList.add("drawer-closed");
    issueDrawerKicker.textContent = "Select an Issue";
    issueDrawerTitle.textContent = "Issue details";
    issueDrawerContent.append(el("p", "muted", "Select an Issue to review evidence and available owner actions."));
    return;
  }
  issueDrawer.classList.add("is-open");
  issueDrawer.setAttribute("aria-hidden", "false");
  issueDrawer.inert = false;
  issueDrawer.dataset.issueCard = String(item.number || "");
  workbenchBody.classList.remove("drawer-closed");
  issueDrawerKicker.textContent = laneLabel(item);
  issueDrawerTitle.textContent = "#" + item.number + " " + (item.title || "Untitled Issue");
  if (item.lane === "blocked" || item.lane === "quarantine") {
    const question = el("div", "owner-question " + (item.lane === "quarantine" ? "system-quarantine" : ""));
    question.append(el("span", "question-label", item.lane === "quarantine" ? "System quarantine" : "Owner question"), el("p", "", item.question || item.reason || "Owner input required"));
    issueDrawerContent.append(question);
  }
  if (item.lane === "running") {
    const meta = el("div", "runtime-meta");
    meta.append(metaItem("Phase", item.display_phase || item.stage || item.status || "In progress"), metaItem("Model", modelName(item.model)), elapsedItem(item.started_at), metaItem("Turns", number(item.turn_count)));
    issueDrawerContent.append(meta, evidenceRow(item), usageRow(item.usage));
  } else if (item.lane === "ready") {
    issueDrawerContent.append(evidenceRow(item), usageRow(item.usage));
  } else if (item.lane === "queue") {
    issueDrawerContent.append(el("p", "drawer-summary", item.status === "Ready for AI" ? "Ready for AI: eligible to start when intake and GitHub state allow it." : "Backlog: not promoted until it is Ready for AI."));
  }
  const actions = el("div", "row-actions");
  const githubFresh = currentSnapshot?.sources?.github?.status === "fresh";
  const testFresh = currentSnapshot?.sources?.test?.status === "fresh";
  if (item.issue_url) actions.append(externalLink(item.issue_url, "Open Issue", "button secondary"));
  if (item.lane === "quarantine" || item.lane === "queue") {
    actions.append(actionButton("Start", "run", "primary", !githubFresh, item.number));
    if (!githubFresh) issueDrawerContent.append(actionHint("Start is unavailable until GitHub state is fresh."));
  }
  if (item.lane === "ready") {
    const itemTest = item.test || currentSnapshot?.test || {};
    const acceptDisabled = !githubFresh || !testFresh || currentSnapshot?.test?.synced !== true || itemTest.synced !== true;
    actions.append(externalLink(itemTest.url, "Open TEST", "button secondary"), actionButton("Accept", "accept", "primary", acceptDisabled, item.number), actionButton("Rework", "rework", "secondary", !githubFresh, item.number));
    if (!githubFresh) {
      issueDrawerContent.append(actionHint("Actions are unavailable until GitHub state is fresh."));
    } else if (!testFresh) {
      issueDrawerContent.append(actionHint("Acceptance is unavailable until TEST evidence is fresh."));
    } else if (currentSnapshot?.test?.synced !== true) {
      issueDrawerContent.append(actionHint("Acceptance is unavailable: TEST is not on the canonical SHA."));
    } else if (itemTest.synced !== true) {
      issueDrawerContent.append(actionHint("Acceptance is unavailable: this Issue's TEST evidence is not synced."));
    }
  }
  if (actions.childElementCount) issueDrawerContent.append(actions);
}

function renderWorkChart(snapshot) {
  if (typeof Chart !== "function") return;
  const counts = snapshot.counts || {};
  const data = [number(counts.blocked), number(counts.running), number(counts.ready_for_acceptance), number(counts.ready_for_ai) + number(counts.backlog)];
  const signature = JSON.stringify([data, document.documentElement.dataset.theme]);
  if (signature === workChartSignature) return;
  if (workChart) workChart.destroy();
  workChart = new Chart(document.getElementById("owner-work-chart"), {
    type: "doughnut",
    data: { labels: ["Needs owner", "Running", "Ready", "Queue"], datasets: [{ data, backgroundColor: ["#b65742", "#64748b", "#167a54", "#9a7a30"], borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: { legend: { display: false }, tooltip: { displayColors: false } } },
  });
  workChartSignature = signature;
}

function narrowWorkbench() {
  return window.matchMedia?.("(max-width: 760px)").matches === true;
}

function laneLabel(item) {
  if (item.lane === "blocked") return "Needs owner";
  if (item.lane === "quarantine") return "System quarantine";
  if (item.lane === "running") return "Running";
  if (item.lane === "ready") return "Ready for Acceptance";
  return item.status === "Ready for AI" ? "Ready for AI" : "Backlog";
}

function renderSources(snapshot) {
  clear(targets.sourceHealth);
  const list = el("div", "source-grid");
  for (const [name, source] of Object.entries(snapshot.sources || {})) {
    const row = el("div", "source-row");
    row.append(el("strong", "", sourceName(name)), badge(titleCase(source.status || "unknown"), source.status === "fresh" ? "good" : "warning"));
    if (source.error) row.append(el("span", "source-error", source.error));
    list.append(row);
  }
  targets.sourceHealth.append(list);
}

function renderWorkDiagnostics(snapshot) {
  clear(targets.diagnosticWork);
  const owner = snapshot.owner_view || {};
  const followUps = owner.follow_ups || [];
  const projectOnly = owner.diagnostics?.project_only_in_progress || [];
  const groups = [
    ["Retrying / delivery follow-ups", followUps, "Claimed Symphony work waiting for its next turn or delivery event. It does not occupy a worker while listed here."],
    ["Project-only In Progress", projectOnly, "GitHub lifecycle state without a live worker or queued Symphony continuation. Review here without inflating the worker count."],
  ];

  for (const [title, items, description] of groups) {
    const section = el("section", "diagnostic-group");
    const heading = el("div", "diagnostic-heading");
    heading.append(el("h3", "", title), badge(String(items.length), items.length ? "warning" : "neutral"));
    section.append(heading, el("p", "diagnostic-description", description));
    if (!items.length) {
      section.append(el("p", "muted diagnostic-empty", "None"));
    } else {
      const list = el("div", "diagnostic-list");
      for (const item of items.slice(0, 10)) list.append(diagnosticWorkRow(item));
      if (items.length > 10) list.append(el("p", "muted diagnostic-more", `+${items.length - 10} more in the machine snapshot`));
      section.append(list);
    }
    targets.diagnosticWork.append(section);
  }
}

function diagnosticWorkRow(item) {
  const row = el("div", "diagnostic-row");
  const copy = el("div", "diagnostic-copy");
  copy.append(externalLink(item.issue_url, `#${item.number} ${item.title || "Untitled Issue"}`, "issue-link"));
  const retryDetails = [item.error, item.deferred_reason].filter((value, index, values) => value && values.indexOf(value) === index);
  const detail = retryDetails.length ? retryDetails.join(" · ") : item.reason || item.stage || item.status;
  if (detail) copy.append(el("span", "diagnostic-reason", detail));
  const attempt = Number(item.attempt);
  row.append(copy, Number.isFinite(attempt) && attempt > 0 ? badge(`Attempt ${attempt}`, "warning") : badge(item.stage || item.status || "In Progress", "neutral"));
  return row;
}

function evidenceRow(item) {
  const row = el("div", "evidence-row");
  const pr = item.pr || {};
  const ci = item.ci || {};
  const test = item.test || {};
  row.append(
    evidenceLink(pr.url, pr.number ? `PR #${pr.number}` : "PR —", pr.url ? "neutral" : "muted"),
    evidenceLink(ci.url, `CI ${titleCase(ci.status || "unknown")}`, statusTone(ci.status)),
    evidenceLink(test.url, `TEST ${titleCase(test.status || (test.synced ? "synced" : "unknown"))}`, test.synced ? "good" : statusTone(test.status)),
  );
  return row;
}

function usageRow(usage) {
  const row = el("div", "usage-row");
  if (!usage) {
    row.append(el("span", "muted", "Task usage unavailable"));
    return row;
  }
  row.append(el("span", "", `${formatNumber(usage.total_tokens)} tokens`), el("span", "", estimatedCredits(usage.estimated_credits_micros)));
  const impact = el("span", "week-impact");
  if (typeof usage.week_impact_percent === "number") {
    impact.textContent = `Week impact ≈ ${formatPercent(usage.week_impact_percent)}`;
    impact.title = "Approximation from observed account-wide weekly quota movement, allocated by task credits. Concurrency prevents exact attribution.";
  } else {
    impact.textContent = "Weekly impact unavailable";
    impact.title = "Account-wide quota observations are insufficient for a responsible task attribution.";
  }
  row.append(impact);
  const details = el("details", "usage-details");
  const summary = el("summary", "", "Usage details");
  const breakdown = el("div", "usage-breakdown");
  for (const [label, value] of [
    ["Input", usage.input_tokens], ["Cached input", usage.cached_input_tokens], ["Cache write", usage.cache_write_input_tokens],
    ["Output", usage.output_tokens], ["Reasoning", usage.reasoning_output_tokens],
  ]) breakdown.append(metaItem(label, formatNumber(value)));
  details.append(summary, breakdown);
  row.append(details);
  return row;
}

function quotaWindow(label, value) {
  const block = el("div", "quota-window");
  const heading = el("div", "quota-heading");
  heading.append(el("strong", "", label), el("span", "", value ? `${formatPercent(value.used_percent)} used` : "Unavailable"));
  const progress = el("div", "quota-track");
  progress.setAttribute("role", "progressbar");
  progress.setAttribute("aria-label", `${label} Codex quota used`);
  progress.setAttribute("aria-valuemin", "0");
  progress.setAttribute("aria-valuemax", "100");
  if (value && Number.isFinite(Number(value.used_percent))) {
    progress.setAttribute("aria-valuenow", String(Number(value.used_percent)));
    progress.setAttribute("aria-valuetext", `${formatPercent(value.used_percent)} used`);
  } else {
    progress.setAttribute("aria-valuetext", "Unavailable");
  }
  const fill = el("span", "quota-fill");
  fill.style.width = `${Math.min(Math.max(Number(value?.used_percent || 0), 0), 100)}%`;
  progress.append(fill);
  block.append(heading, progress, el("small", "muted", value?.resets_at ? `Resets ${formatReset(value.resets_at)}` : "Waiting for Codex rate limits"));
  return block;
}

function deliveryRow(label, sha, url, status, tone = "neutral") {
  const row = el("div", "delivery-row");
  const copy = el("div", "delivery-copy");
  copy.append(el("span", "muted", label), externalLink(url, shortSha(sha), "sha-link"));
  row.append(copy, badge(status, tone));
  return row;
}

function fact(label, value, tone) {
  const node = el("div", `service-fact ${tone}`);
  node.append(el("span", "", label), el("strong", "", value));
  return node;
}

function actionButton(label, action, variant = "secondary", disabled = false, issue = null) {
  const button = el("button", `button ${variant}`, label);
  button.type = "button";
  button.dataset.action = action;
  if (issue !== null && issue !== undefined) button.dataset.issue = String(issue);
  button.disabled = disabled || actionInFlight;
  return button;
}

function actionHint(message) {
  const hint = el("p", "action-hint", message);
  hint.setAttribute("role", "status");
  return hint;
}

function externalLink(url, label, className = "") {
  if (!url) return el("span", `${className} is-disabled`.trim(), label);
  const link = el("a", className, label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noreferrer";
  return link;
}

function evidenceLink(url, label, tone) { return externalLink(url, label, `evidence-chip ${tone}`); }
function badge(label, tone = "neutral") { return el("span", `badge ${tone}`, label); }

function metaItem(label, value) {
  const item = el("div", "meta-item");
  item.append(el("span", "", label), el("strong", "", value ?? "—"));
  return item;
}

function elapsedItem(startedAt) {
  const item = metaItem("Elapsed", elapsed(startedAt));
  const value = item.querySelector("strong");
  if (startedAt) value.dataset.startedAt = startedAt;
  return item;
}

function emptyState(title, detail) {
  const state = el("div", "empty-state");
  state.append(el("strong", "", title), el("span", "", detail));
  return state;
}

function openActionDialog(action, issue) {
  if (actionInFlight || actionDialog.open || !currentSnapshot) return;
  const item = issue ? findOwnerItem(issue) || currentSnapshot.issues?.[String(issue)] : null;
  const testEvidence = item?.test || currentSnapshot.test || {};
  const workers = number(currentSnapshot.workers?.running);
  const definitions = {
    pause: ["Pause intake", "New Issues will not be claimed. Running workers can finish.", "Pause intake", "primary"],
    resume: ["Resume intake", "Symphony may claim eligible Ready for AI Issues up to available capacity.", "Resume intake", "primary"],
    start_service: ["Start Symphony", "Owner Control and Telegram stay online while the fixed Symphony service starts.", "Start Symphony", "primary"],
    stop_service: ["Stop Symphony", workers > 0 ? `This will interrupt ${workers} running worker${workers === 1 ? "" : "s"}. Pause intake is the safe alternative.` : "No running workers will be interrupted.", workers > 0 ? `Stop and interrupt ${workers}` : "Stop Symphony", "danger"],
    restart: ["Restart Symphony", workers > 0 ? `${workers} running worker${workers === 1 ? "" : "s"} may be interrupted.` : "The runtime will briefly become unavailable; Owner Control remains online.", "Restart Symphony", "danger"],
    run: [`Start #${issue}`, `${item?.title || "This Issue"} will move to Ready for AI and receive the Symphony lease.`, "Start Issue", "primary"],
    accept: [`Accept #${issue}`, `Close the Issue as Done after verifying TEST ${shortSha(testEvidence.sha)} matches canonical.`, "Accept as Done", "primary"],
    rework: [`Rework #${issue}`, "Add a short owner reason. The Issue returns to Ready for AI with the Symphony lease.", "Send to rework", "primary"],
  };
  const definition = definitions[action];
  if (!definition) return;
  pendingAction = { action, issue, workers, testEvidence };
  dialogTitle.textContent = definition[0];
  clear(dialogBody);
  dialogBody.append(el("p", "dialog-copy", definition[1]));
  if (action === "accept") dialogBody.append(deliveryRow("TEST SHA", testEvidence.sha, testEvidence.url, testEvidence.synced ? "Synced" : testEvidence.drift ? "Drift" : "Ready", testEvidence.synced ? "good" : testEvidence.drift ? "warning" : "neutral"));
  if (action === "rework") {
    const label = el("label", "field-label", "Reason");
    const textarea = el("textarea", "reason-field");
    textarea.id = "rework-reason";
    textarea.name = "reason";
    textarea.rows = 4;
    textarea.required = true;
    textarea.setAttribute("aria-describedby", "dialog-error");
    textarea.placeholder = "What should change before acceptance?";
    textarea.addEventListener("invalid", (event) => {
      event.preventDefault();
      showDialogError("A short rework reason is required.");
    });
    label.htmlFor = textarea.id;
    dialogBody.append(label, textarea);
  }
  dialogConfirm.textContent = definition[2];
  dialogConfirm.className = `button ${definition[3]}`;
  actionDialog.showModal();
  if (action === "rework") document.getElementById("rework-reason").focus();
}

async function performPendingAction() {
  if (!pendingAction || actionInFlight) return;
  const { action, issue, workers } = pendingAction;
  const params = {};
  if (issue) params.issue = Number(issue);
  if (action === "stop_service" && workers > 0) params.confirm_running_workers = workers;
  if (action === "rework") {
    const reason = document.getElementById("rework-reason")?.value.trim();
    if (!reason) {
      showDialogError("A short rework reason is required.");
      return;
    }
    params.reason = reason;
  }
  actionErrors.delete(issue ? String(issue) : "service");
  actionDialog.close();
  pendingAction = null;
  actionInFlight = true;
  app.classList.add("action-busy");
  render(currentSnapshot, { force: true });
  try {
    const response = await fetch(`/ui/actions/${action}`, {
      method: "POST",
      headers: browserHeaders(true),
      body: JSON.stringify(params),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || `Action rejected (${response.status})`);
    toast("Action accepted. Confirming state…");
    await refreshSnapshot({ announce: false });
  } catch (error) {
    showInlineActionError(issue, error.message);
    toast(error.message, "error");
  } finally {
    actionInFlight = false;
    app.classList.remove("action-busy");
    if (currentSnapshot) render(currentSnapshot, { force: true });
  }
}

function showInlineActionError(issue, message) {
  actionErrors.set(issue ? String(issue) : "service", message);
  renderActionErrors();
}

function renderActionErrors() {
  document.querySelectorAll(".action-error[data-persistent]").forEach((node) => node.remove());
  for (const [key, message] of actionErrors) {
    const target = key === "service"
      ? document.querySelector(".service-panel")
      : String(issueDrawer.dataset.issueCard || "") === key
        ? issueDrawerContent
        : null;
    if (!target) continue;
    const error = el("div", "action-error", message);
    error.dataset.persistent = "true";
    error.setAttribute("role", "alert");
    target.append(error);
  }
}

function showDialogError(message) {
  dialogBody.querySelector(".action-error")?.remove();
  const error = el("div", "action-error", message);
  error.id = "dialog-error";
  error.setAttribute("role", "alert");
  dialogBody.append(error);
  const textarea = document.getElementById("rework-reason");
  if (textarea) textarea.setAttribute("aria-invalid", "true");
}

function toast(message, tone = "neutral") {
  const node = el("div", `toast ${tone}`, message);
  toastRegion.append(node);
  window.setTimeout(() => node.remove(), 4200);
}

async function loadLogs() {
  targets.logs.textContent = "Loading recent logs…";
  try {
    const response = await fetch("/ui/logs?tail=80", { headers: browserHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error(`Logs unavailable (${response.status})`);
    const body = await response.json();
    targets.logs.textContent = (body.lines || []).join("\n") || "No recent log lines.";
  } catch (error) {
    targets.logs.textContent = error.message;
  }
}

function showStaleError(message) {
  snapshotStatus.textContent = currentSnapshot ? "Last confirmed snapshot" : "Owner Control unavailable";
  snapshotStatus.className = "status-chip is-stale";
  globalNotice.textContent = currentSnapshot ? `${message}. Showing the last confirmed snapshot.` : `${message}. No control data is available.`;
  globalNotice.className = "notice is-error-notice";
}

function renderUnavailable(message) {
  renderedSignature = null;
  const serviceTitle = document.getElementById("service-title");
  serviceTitle.textContent = "Owner Control unavailable";
  serviceTitle.className = "service-stopped";
  clear(targets.serviceFacts);
  targets.serviceFacts.append(fact("State", "Unavailable", "danger"));
  clear(targets.serviceActions);
  const retry = el("button", "button primary", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => refreshSnapshot());
  targets.serviceActions.append(retry);
  targets.delivery.classList.remove("loading-block");
  targets.delivery.removeAttribute("role");
  targets.delivery.removeAttribute("aria-label");
  clear(targets.delivery);
  targets.delivery.append(emptyState("Delivery unavailable", message));
  targets.quota.classList.remove("loading-block");
  targets.quota.removeAttribute("role");
  targets.quota.removeAttribute("aria-label");
  clear(targets.quota);
  targets.quota.append(emptyState("Quota unavailable", "Waiting for Owner Control data."));
  clear(issueTableBody);
  const row = document.createElement("tr");
  const cell = el("td", "empty-table-cell", "Unavailable. Try again when the local control source recovers.");
  cell.colSpan = 4;
  row.append(cell);
  issueTableBody.append(row);
  showMoreButton.classList.add("is-hidden");
  renderDrawer(null);
  clear(targets.sourceHealth);
  targets.sourceHealth.append(emptyState("No source health", "The snapshot endpoint did not respond."));
  clear(targets.diagnosticWork);
}

function snapshotSignature(snapshot) {
  const stable = JSON.parse(JSON.stringify(snapshot || {}));
  delete stable.generated_at;
  delete stable.refreshed_at;
  if (stable.owner_view) delete stable.owner_view.updated_at;
  for (const source of Object.values(stable.sources || {})) {
    if (source && typeof source === "object") delete source.confirmed_at;
  }
  return JSON.stringify(stable);
}

function captureViewState() {
  const openUsage = new Set();
  for (const details of document.querySelectorAll("details.usage-details[open]")) {
    const issue = details.closest("[data-issue-card]")?.dataset.issueCard;
    if (issue) openUsage.add(issue);
  }
  const active = document.activeElement;
  const focus = active?.dataset?.action
    ? { action: active.dataset.action, issue: active.dataset.issue || "" }
    : active?.id
      ? { id: active.id }
      : null;
  return {
    openUsage,
    serviceAdvancedOpen: Boolean(document.querySelector("details.service-advanced[open]")),
    focus,
  };
}

function restoreViewState(state) {
  if (!state) return;
  for (const issue of state.openUsage) {
    const details = document.querySelector(`[data-issue-card="${CSS.escape(issue)}"] details.usage-details`);
    if (details) details.open = true;
  }
  const advanced = document.querySelector("details.service-advanced");
  if (advanced) advanced.open = state.serviceAdvancedOpen;
  if (state.focus?.action) {
    const issueSelector = state.focus.issue
      ? `[data-issue="${CSS.escape(state.focus.issue)}"]`
      : ":not([data-issue])";
    const selector = `button[data-action="${CSS.escape(state.focus.action)}"]${issueSelector}`;
    document.querySelector(selector)?.focus({ preventScroll: true });
  } else if (state.focus?.id) {
    document.getElementById(state.focus.id)?.focus({ preventScroll: true });
  }
}

function findOwnerItem(issue) {
  return findWorkbenchItem(currentSnapshot || {}, issue);
}

function clear(node) { node.replaceChildren(); }
function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}
function number(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function formatNumber(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("en").format(Number(value)) : "—"; }
function formatPercent(value) { return `${new Intl.NumberFormat("en", { maximumFractionDigits: 1 }).format(Number(value))}%`; }
function shortSha(value) { return value ? String(value).slice(0, 8) : "Unknown"; }
function estimatedCredits(micros) { return Number.isFinite(Number(micros)) ? `Est. ${(Number(micros) / 1_000_000).toLocaleString("en", { maximumFractionDigits: 3 })} credits` : "Estimated credits unavailable"; }
function titleCase(value) { const text = String(value || "").replaceAll("_", " "); return text ? text[0].toUpperCase() + text.slice(1) : "Unknown"; }
function modelName(model) { return titleCase(model?.selected_tier || model?.tier || "unknown"); }
function sourceName(name) { return ({ github: "GitHub", test: "TEST", runtime: "Runtime", supervisor: "Supervisor" })[name] || titleCase(name); }
function statusTone(value) { const status = String(value || "").toLowerCase(); if (["success", "synced", "ready for ai"].includes(status)) return "good"; if (["failure", "failed", "blocked"].includes(status)) return "danger"; if (["pending", "waiting", "retrying"].includes(status)) return "warning"; return "neutral"; }
function formatReset(epoch) { const value = Number(epoch) * 1000; return Number.isFinite(value) ? new Intl.DateTimeFormat("en", { weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "unknown"; }
function relativeTime(value) { const timestamp = Date.parse(value); if (!Number.isFinite(timestamp)) return "now"; const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000)); if (seconds < 5) return "now"; if (seconds < 60) return `${seconds}s ago`; const minutes = Math.round(seconds / 60); return minutes < 60 ? `${minutes}m ago` : `${Math.round(minutes / 60)}h ago`; }
function elapsed(value) { const timestamp = Date.parse(value); if (!Number.isFinite(timestamp)) return "—"; const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000)); const hours = Math.floor(seconds / 3600); const minutes = Math.floor((seconds % 3600) / 60); return hours ? `${hours}h ${minutes}m` : `${minutes}m`; }

function activateOwnerTab(key, focus = false) {
  activeTab = key;
  activeTabInitialized = true;
  selectedIssue = null;
  drawerDismissed = false;
  visibleRows = TABLE_PAGE_SIZE;
  renderWorkbench(currentSnapshot || {});
  renderActionErrors();
  if (focus) ownerTabs.querySelector(`[data-owner-tab="${key}"]`)?.focus({ preventScroll: true });
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-owner-tab]");
  if (tab) {
    activateOwnerTab(tab.dataset.ownerTab, true);
    return;
  }
  const select = event.target.closest("[data-issue-select]");
  if (select) {
    selectWorkbenchIssue(select.dataset.issueSelect);
    renderActionErrors();
    return;
  }
  const button = event.target.closest("button[data-action]");
  if (button) openActionDialog(button.dataset.action, button.dataset.issue || null);
});
ownerTabs.addEventListener("keydown", (event) => {
  const tab = event.target.closest("[data-owner-tab]");
  if (!tab) return;
  const tabs = [...ownerTabs.querySelectorAll("[data-owner-tab]")];
  const currentIndex = tabs.indexOf(tab);
  let nextIndex = null;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % tabs.length;
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = tabs.length - 1;
  if (nextIndex === null) return;
  event.preventDefault();
  activateOwnerTab(tabs[nextIndex].dataset.ownerTab, true);
});
themeToggle.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
showMoreButton.addEventListener("click", () => {
  visibleRows += TABLE_PAGE_SIZE;
  renderWorkbench(currentSnapshot || {});
});
document.getElementById("issue-drawer-close").addEventListener("click", () => {
  drawerDismissed = true;
  selectedIssue = null;
  for (const row of document.querySelectorAll("[data-issue-row]")) {
    row.setAttribute("aria-selected", "false");
    row.querySelector("[data-issue-select]")?.setAttribute("aria-expanded", "false");
  }
  renderDrawer(null);
});
dialogForm.addEventListener("submit", (event) => { event.preventDefault(); performPendingAction(); });
document.getElementById("dialog-close").addEventListener("click", () => actionDialog.close());
document.getElementById("dialog-cancel").addEventListener("click", () => actionDialog.close());
actionDialog.addEventListener("close", () => { if (!actionInFlight) pendingAction = null; });
refreshButton.addEventListener("click", () => refreshSnapshot());
document.getElementById("runtime-diagnostics").addEventListener("toggle", (event) => { if (event.target.open) loadLogs(); });
window.setInterval(() => document.querySelectorAll("[data-started-at]").forEach((node) => { node.textContent = elapsed(node.dataset.startedAt); }), 1000);
window.setInterval(() => refreshSnapshot({ announce: false }), 5000);
setTheme(initialTheme(), false);
refreshSnapshot();
