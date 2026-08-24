"use strict";

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

runtimeLink.href = runtimeUrl;

let currentSnapshot = null;
let requestInFlight = false;
let actionInFlight = false;
let pendingAction = null;
let renderedSignature = null;
const actionErrors = new Map();

const targets = {
  serviceFacts: document.getElementById("service-facts"),
  serviceActions: document.getElementById("service-actions"),
  delivery: document.getElementById("delivery-content"),
  quota: document.getElementById("quota-content"),
  counters: document.getElementById("counter-strip"),
  needsOwner: document.getElementById("needs-owner-content"),
  running: document.getElementById("running-content"),
  ready: document.getElementById("ready-content"),
  backlog: document.getElementById("backlog-content"),
  sourceHealth: document.getElementById("source-health"),
  logs: document.getElementById("diagnostic-logs"),
};

function browserHeaders(json = false) {
  const headers = { "X-Owner-Control-CSRF": csrf };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
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
  renderDelivery(snapshot);
  renderQuota(snapshot);
  renderCounters(snapshot);
  renderBlocked(snapshot);
  renderRunning(snapshot);
  renderReady(snapshot);
  renderBacklog(snapshot);
  renderSources(snapshot);
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
    globalNotice.textContent = `Last confirmed data remains visible. Waiting for ${names || "a source"}.`;
    globalNotice.className = "notice is-stale-notice";
  } else {
    globalNotice.className = "notice is-hidden";
    globalNotice.textContent = "";
  }
}

function renderService(snapshot) {
  const service = snapshot.service || {};
  const runtimeSource = snapshot.sources?.runtime || {};
  const serviceKnown = service.status !== "unknown";
  const isRunning = service.live === true;
  const serviceTitle = document.getElementById("service-title");
  serviceTitle.textContent = serviceKnown ? (isRunning ? "Running" : "Stopped") : "Status unavailable";
  serviceTitle.className = isRunning ? "service-running" : "service-stopped";

  clear(targets.serviceFacts);
  targets.serviceFacts.append(
    fact("Service", serviceKnown ? (isRunning ? "Running" : "Stopped") : "Unknown", isRunning ? "good" : "danger"),
    fact("Intake", snapshot.intake?.active ? "Active" : "Paused", snapshot.intake?.active ? "good" : "warning"),
    fact("Workers", `${number(snapshot.workers?.running)}/${number(snapshot.workers?.limit)}`, "neutral"),
    fact("Runtime API", titleCase(runtimeSource.status || "unknown"), runtimeSource.status === "fresh" ? "good" : "warning"),
  );

  clear(targets.serviceActions);
  const supervisorFresh = snapshot.sources?.supervisor?.status === "fresh";
  const runtimeFresh = snapshot.sources?.runtime?.status === "fresh";
  const githubFresh = snapshot.sources?.github?.status === "fresh";
  if (isRunning) {
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

function renderCounters(snapshot) {
  clear(targets.counters);
  const counts = snapshot.counts || {};
  const definitions = [
    ["Ready for AI", counts.ready_for_ai, "#backlog-title", "neutral"],
    ["Running", counts.running, "#running-title", "neutral"],
    ["Blocked", counts.blocked, "#needs-owner-title", number(counts.blocked) > 0 ? "danger" : "neutral"],
    ["Ready for Acceptance", counts.ready_for_acceptance, "#ready-title", number(counts.ready_for_acceptance) > 0 ? "good" : "neutral"],
    ["Backlog", counts.backlog, "#backlog-title", "neutral"],
  ];
  for (const [label, value, href, tone] of definitions) {
    const counter = el("a", `counter ${tone}`);
    counter.href = href;
    counter.append(el("strong", "", number(value)), el("span", "", label));
    targets.counters.append(counter);
  }
}

function renderBlocked(snapshot) {
  clear(targets.needsOwner);
  const items = snapshot.owner_view?.blocked || [];
  if (!items.length) return targets.needsOwner.append(emptyState("No owner decisions", "Blocked questions will appear here."));
  const list = el("div", "issue-list attention-list");
  for (const item of items) {
    const card = issueCard(item, "blocked");
    const question = el("div", "owner-question");
    question.append(el("span", "question-label", "Owner question"), el("p", "", item.question || item.reason || "Owner input required"));
    const actions = el("div", "row-actions");
    actions.append(externalLink(item.issue_url, "Open Issue", "button secondary"));
    card.append(question, actions);
    list.append(card);
  }
  targets.needsOwner.append(list);
}

function renderRunning(snapshot) {
  clear(targets.running);
  const items = snapshot.owner_view?.work_items || [];
  if (!items.length) return targets.running.append(emptyState("No active work", snapshot.intake?.active ? "Ready tasks will start when capacity is available." : "Intake is paused."));
  const list = el("div", "issue-list running-list");
  for (const item of items) {
    const card = issueCard(item, "running");
    const meta = el("div", "runtime-meta");
    meta.append(
      metaItem("Stage", item.stage || item.status || "In progress"),
      metaItem("Model", modelName(item.model)),
      elapsedItem(item.started_at),
      metaItem("Turns", number(item.turn_count)),
    );
    card.append(meta, evidenceRow(item), usageRow(item.usage));
    list.append(card);
  }
  targets.running.append(list);
}

function renderReady(snapshot) {
  clear(targets.ready);
  const items = snapshot.owner_view?.ready_for_acceptance || [];
  document.getElementById("ready-title").textContent = items.length ? `Ready for Acceptance · ${items.length}` : "Ready for Acceptance";
  if (!items.length) return targets.ready.append(emptyState("Nothing waiting for acceptance", "Completed delivery evidence will appear here."));
  const list = el("div", "issue-list");
  const githubFresh = snapshot.sources?.github?.status === "fresh";
  const testFresh = snapshot.sources?.test?.status === "fresh";
  const previewCount = 5;
  for (const item of items.slice(0, previewCount)) list.append(readyCard(item, snapshot, githubFresh, testFresh));
  if (items.length > previewCount) {
    const more = el("details", "ready-more");
    more.append(el("summary", "", `Show ${items.length - previewCount} more`));
    const remaining = el("div", "issue-list ready-more-list");
    for (const item of items.slice(previewCount)) remaining.append(readyCard(item, snapshot, githubFresh, testFresh));
    more.append(remaining);
    list.append(more);
  }
  targets.ready.append(list);
}

function readyCard(item, snapshot, githubFresh, testFresh) {
  const card = issueCard(item, "ready");
  card.append(evidenceRow(item), usageRow(item.usage));
  const actions = el("div", "row-actions");
  const itemTest = item.test || snapshot.test || {};
  const itemTestSynced = itemTest.synced === true;
  const globalTestSynced = snapshot.test?.synced === true;
  const acceptDisabled = !githubFresh || !testFresh || !globalTestSynced || !itemTestSynced;
  const accept = actionButton("Accept", "accept", "primary", acceptDisabled, item.number);
  const rework = actionButton("Rework", "rework", "secondary", !githubFresh, item.number);
  actions.append(
    externalLink(itemTest.url, "Open TEST", "button secondary"),
    accept,
    rework,
  );
  card.append(actions);
  if (!githubFresh) {
    card.append(actionHint("Actions are unavailable until GitHub state is fresh."));
  } else if (!testFresh) {
    card.append(actionHint("Acceptance is unavailable until TEST evidence is fresh."));
  } else if (!globalTestSynced) {
    card.append(actionHint("Acceptance is unavailable: TEST is not on the canonical SHA."));
  } else if (!itemTestSynced) {
    card.append(actionHint("Acceptance is unavailable: this Issue's TEST evidence is not synced."));
  }
  return card;
}

function renderBacklog(snapshot) {
  clear(targets.backlog);
  const items = snapshot.owner_view?.backlog || [];
  if (!items.length) return targets.backlog.append(emptyState("Backlog is clear", "No unclaimed Symphony Issues."));
  const list = el("div", "compact-list");
  const githubFresh = snapshot.sources?.github?.status === "fresh";
  const previewCount = 5;
  for (const item of items.slice(0, previewCount)) list.append(backlogRow(item, snapshot, githubFresh));
  if (items.length > previewCount) {
    const more = el("details", "backlog-more");
    more.append(el("summary", "", `Show ${items.length - previewCount} more`));
    const remaining = el("div", "compact-list backlog-more-list");
    for (const item of items.slice(previewCount)) remaining.append(backlogRow(item, snapshot, githubFresh));
    more.append(remaining);
    list.append(more);
  }
  if (!githubFresh) list.append(actionHint("Start is unavailable until GitHub state is fresh."));
  targets.backlog.append(list);
}

function backlogRow(item, snapshot, githubFresh) {
  const row = el("article", "compact-row");
  row.dataset.issueCard = String(item.number || "");
  const copy = el("div", "compact-copy");
  copy.append(externalLink(item.issue_url, `#${item.number} ${item.title || "Untitled Issue"}`, "issue-link"), badge(item.status || item.stage || "Backlog", statusTone(item.status)));
  const start = actionButton("Start", "run", "primary", !githubFresh, item.number);
  if (!snapshot.intake?.active) start.title = "Issue will become Ready for AI and wait until intake resumes.";
  row.append(copy, start);
  return row;
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

function issueCard(item, tone) {
  const card = el("article", `issue-card ${tone}`);
  card.dataset.issueCard = String(item.number || "");
  const heading = el("div", "issue-heading");
  const copy = el("div", "issue-copy");
  copy.append(externalLink(item.issue_url, `#${item.number}`, "issue-number"), el("h3", "", item.title || "Untitled Issue"));
  heading.append(copy, badge(item.stage || item.status || titleCase(tone), tone === "blocked" ? "danger" : tone === "ready" ? "good" : "neutral"));
  card.append(heading);
  return card;
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
    const selector = key === "service" ? ".service-panel" : `[data-issue-card="${CSS.escape(key)}"]`;
    const target = document.querySelector(selector) || globalNotice;
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
  clear(targets.delivery);
  targets.delivery.append(emptyState("Delivery unavailable", message));
  clear(targets.quota);
  targets.quota.append(emptyState("Quota unavailable", "Waiting for Owner Control data."));
  clear(targets.counters);
  for (const target of [targets.needsOwner, targets.running, targets.ready, targets.backlog]) {
    clear(target);
    target.append(emptyState("Unavailable", "Try again when the local control source recovers."));
  }
  clear(targets.sourceHealth);
  targets.sourceHealth.append(emptyState("No source health", "The snapshot endpoint did not respond."));
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
    readyMoreOpen: Boolean(document.querySelector("details.ready-more[open]")),
    backlogMoreOpen: Boolean(document.querySelector("details.backlog-more[open]")),
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
  const readyMore = document.querySelector("details.ready-more");
  if (readyMore) readyMore.open = state.readyMoreOpen;
  const backlogMore = document.querySelector("details.backlog-more");
  if (backlogMore) backlogMore.open = state.backlogMoreOpen;
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
  const owner = currentSnapshot?.owner_view || {};
  for (const lane of ["blocked", "work_items", "ready_for_acceptance", "backlog"]) {
    const found = (owner[lane] || []).find((item) => String(item.number) === String(issue));
    if (found) return found;
  }
  return null;
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

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button) openActionDialog(button.dataset.action, button.dataset.issue || null);
});
dialogForm.addEventListener("submit", (event) => { event.preventDefault(); performPendingAction(); });
document.getElementById("dialog-close").addEventListener("click", () => actionDialog.close());
document.getElementById("dialog-cancel").addEventListener("click", () => actionDialog.close());
actionDialog.addEventListener("close", () => { if (!actionInFlight) pendingAction = null; });
refreshButton.addEventListener("click", () => refreshSnapshot());
document.getElementById("runtime-diagnostics").addEventListener("toggle", (event) => { if (event.target.open) loadLogs(); });
window.setInterval(() => document.querySelectorAll("[data-started-at]").forEach((node) => { node.textContent = elapsed(node.dataset.startedAt); }), 1000);
window.setInterval(() => refreshSnapshot({ announce: false }), 5000);
refreshSnapshot();
