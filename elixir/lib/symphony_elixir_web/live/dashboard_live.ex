defmodule SymphonyElixirWeb.DashboardLive do
  @moduledoc """
  Live observability dashboard for Symphony.
  """

  use Phoenix.LiveView, layout: {SymphonyElixirWeb.Layouts, :app}

  alias SymphonyElixir.OwnerControl.Client, as: OwnerControlClient
  alias SymphonyElixirWeb.{Endpoint, ObservabilityPubSub, Presenter}
  @runtime_tick_ms 1_000
  @control_refresh_ms 5_000

  @impl true
  def mount(_params, _session, socket) do
    socket =
      socket
      |> assign(:payload, load_payload())
      |> assign(:now, DateTime.utc_now())

    if connected?(socket) do
      :ok = ObservabilityPubSub.subscribe()
      schedule_runtime_tick()
      schedule_control_refresh()
    end

    {:ok, socket}
  end

  @impl true
  def handle_info(:runtime_tick, socket) do
    schedule_runtime_tick()
    {:noreply, assign(socket, :now, DateTime.utc_now())}
  end

  def handle_info(:control_refresh, socket) do
    schedule_control_refresh()

    if owner_control_client().enabled?() do
      {:noreply,
       socket
       |> assign(:payload, load_payload())
       |> assign(:now, DateTime.utc_now())}
    else
      {:noreply, socket}
    end
  end

  @impl true
  def handle_info(:observability_updated, socket) do
    {:noreply,
     socket
     |> assign(:payload, load_payload())
     |> assign(:now, DateTime.utc_now())}
  end

  @impl true
  def handle_event("pause-intake", _params, socket), do: control_action(socket, :pause, %{})

  def handle_event("resume-intake", _params, socket), do: control_action(socket, :resume, %{})

  def handle_event("restart-service", _params, socket), do: control_action(socket, :restart, %{})

  def handle_event("run", %{"issue" => issue}, socket) do
    case parse_issue_number(issue) do
      {:ok, issue_number} -> control_action(socket, :run, %{issue: issue_number})
      {:error, _reason} -> {:noreply, put_flash(socket, :error, "Invalid issue number")}
    end
  end

  def handle_event("accept", %{"issue" => issue}, socket) do
    case parse_issue_number(issue) do
      {:ok, issue_number} -> control_action(socket, :accept, %{issue: issue_number})
      {:error, _reason} -> {:noreply, put_flash(socket, :error, "Invalid issue number")}
    end
  end

  def handle_event("rework", %{"issue" => issue, "reason" => reason}, socket) do
    with {:ok, issue_number} <- parse_issue_number(issue),
         normalized_reason when normalized_reason != "" <- String.trim(reason) do
      control_action(socket, :rework, %{issue: issue_number, reason: normalized_reason})
    else
      _error -> {:noreply, put_flash(socket, :error, "Rework reason is required")}
    end
  end

  @impl true
  def render(assigns) do
    ~H"""
    <section class="dashboard-shell">
      <div :if={message = Phoenix.Flash.get(@flash, :info)} class="control-flash control-flash-info">
        <%= message %>
      </div>
      <div :if={message = Phoenix.Flash.get(@flash, :error)} class="control-flash control-flash-error">
        <%= message %>
      </div>
      <header class="hero-card">
        <div class="hero-grid">
          <div>
            <p class="eyebrow">
              Symphony · Owner view
            </p>
            <h1 class="hero-title">
              Owner control
            </h1>
            <p class="hero-copy">
              See what needs attention, what is moving, and what is ready to accept — without reading orchestration logs.
            </p>
          </div>

          <div class="status-stack">
            <%= if control_snapshot?(@payload) do %>
              <span class={service_badge_class(@payload)}>
                <span class="status-badge-dot"></span>
                <%= if service_live?(@payload), do: "Live", else: "Down" %>
              </span>
            <% else %>
              <span class="status-badge status-badge-live">
                <span class="status-badge-dot"></span>
                Live
              </span>
              <span class="status-badge status-badge-offline">
                <span class="status-badge-dot"></span>
                Offline
              </span>
            <% end %>
            <span class="freshness-label mono">
              Updated <%= snapshot_freshness(@payload) %>
            </span>
          </div>
        </div>

        <div :if={control_snapshot?(@payload)} class="control-strip">
          <div class="control-statuses">
            <span class={intake_badge_class(@payload)}>
              Intake <%= if intake_active?(@payload), do: "Active", else: "Paused" %>
            </span>
            <span class="control-worker-count numeric">Workers <%= workers_label(@payload) %></span>
            <span class="routing-status">Routing Auto</span>
            <span class="model-count model-count-luna"><%= model_active_label(@payload, :luna) %></span>
            <span class="model-count model-count-terra"><%= model_active_label(@payload, :terra) %></span>
            <span class="model-count model-count-sol"><%= model_active_label(@payload, :sol) %></span>
          </div>
          <div class="control-actions">
            <button
              :if={intake_active?(@payload)}
              type="button"
              class="control-button"
              phx-click="pause-intake"
            >Pause intake</button>
            <button
              :if={!intake_active?(@payload)}
              type="button"
              class="control-button control-button-primary"
              phx-click="resume-intake"
            >Resume intake</button>
            <button
              type="button"
              class="control-button control-button-danger"
              phx-click="restart-service"
            >Restart service</button>
          </div>
        </div>

        <div :if={control_snapshot?(@payload)} class={release_status_class(@payload)}>
          <span>Canonical: <strong class="mono"><%= compact_sha(@payload.canonical.sha) %></strong></span>
          <span>TEST: <strong class="mono"><%= compact_sha(@payload.test.sha) %></strong></span>
          <span><%= if @payload.test.synced, do: "✓ synced", else: "⚠ drift" %></span>
        </div>
      </header>

      <section :if={@payload[:control_error]} class="error-card control-error-card">
        <h2 class="error-title">Owner controls unavailable</h2>
        <p class="error-copy">
          New work is paused fail-closed. Running workers are not interrupted; use external service controls if needed.
        </p>
      </section>

      <%= if @payload[:error] do %>
        <section class="error-card">
          <h2 class="error-title">
            Snapshot unavailable
          </h2>
          <p class="error-copy">
            <strong><%= @payload.error.code %>:</strong> <%= @payload.error.message %>
          </p>
        </section>
      <% else %>
        <section class="metric-grid">
          <article class="metric-card">
            <p class="metric-label">Backlog</p>
            <p class="metric-value numeric"><%= format_count(@payload.counts.backlog) %></p>
            <p class="metric-detail">Not yet queued.</p>
          </article>

          <article class="metric-card">
            <p class="metric-label">Ready for AI</p>
            <p class="metric-value numeric"><%= format_count(Map.get(@payload.counts, :ready_for_ai)) %></p>
            <p class="metric-detail">Eligible for Start.</p>
          </article>

          <article class="metric-card metric-card-active">
            <p class="metric-label">Running</p>
            <p class="metric-value numeric"><%= @payload.counts.running %></p>
            <p class="metric-detail">Active agent sessions.</p>
          </article>

          <article class="metric-card">
            <p class="metric-label">Queued</p>
            <p class="metric-value numeric"><%= @payload.counts.queued %></p>
            <p class="metric-detail">Waiting for a retry window.</p>
          </article>

          <article class="metric-card metric-card-danger">
            <p class="metric-label">Blocked</p>
            <p class="metric-value numeric"><%= @payload.counts.blocked %></p>
            <p class="metric-detail">Needs owner attention.</p>
          </article>

          <article class="metric-card">
            <p class="metric-label">Ready for Acceptance</p>
            <p class="metric-value numeric"><%= format_count(@payload.counts.ready_for_acceptance) %></p>
            <p class="metric-detail">Deployed to TEST.</p>
          </article>

          <article class="metric-card">
            <p class="metric-label">Done</p>
            <p class="metric-value numeric"><%= format_count(@payload.counts.done) %></p>
            <p class="metric-detail">Accepted and complete.</p>
          </article>
        </section>

        <section class={attention_card_class(@payload.owner_view.blocked)}>
          <div class="section-header">
            <div>
              <p class="section-kicker">Attention first</p>
              <h2 class="section-title">Needs owner</h2>
              <p class="section-copy">Only blockers that require a decision or explicit input.</p>
            </div>
          </div>

          <%= if @payload.owner_view.blocked == [] do %>
            <p class="empty-state">No owner blockers.</p>
          <% else %>
            <div class="attention-list">
              <article :for={entry <- @payload.owner_view.blocked} class="attention-item">
                <div class="attention-issue">
                  <.issue_identifier identifier={entry.issue_identifier} url={entry.issue_url} />
                  <span class="state-badge state-badge-danger">Blocked</span>
                </div>
                <p :if={entry.title} class="attention-title"><%= entry.title %></p>
                <p class="attention-reason"><%= entry.question || entry.reason || "Owner input required" %></p>
                <p :if={entry.reason && entry.reason != entry.question} class="muted"><%= entry.reason %></p>
                <a
                  :if={external_issue_url(entry.issue_url)}
                  class="control-link"
                  href={external_issue_url(entry.issue_url)}
                  target="_blank"
                  rel="noopener noreferrer"
                >Open Issue</a>
              </article>
            </div>
          <% end %>
        </section>

        <section class="section-card section-card-primary">
          <div class="section-header">
            <div>
              <p class="section-kicker">Delivery flow</p>
              <h2 class="section-title">Work in progress</h2>
              <p class="section-copy">Issue, current stage, elapsed time, and delivery evidence in one row.</p>
            </div>
          </div>

          <%= if @payload.owner_view.work_items == [] do %>
            <p class="empty-state">No work is currently in progress.</p>
          <% else %>
            <div class="table-wrap">
              <table class="data-table owner-table">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>Stage</th>
                    <th>Model</th>
                    <th>In work</th>
                    <th>PR / CI</th>
                    <th>TEST</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={item <- @payload.owner_view.work_items}>
                    <td><.owner_issue item={item} /></td>
                    <td><span class={state_badge_class(item.status || item.stage)}><%= item.stage || "Unknown" %></span></td>
                    <td><.model_status model={item[:model]} /></td>
                    <td class="numeric"><%= format_elapsed(item[:started_at], @now) %></td>
                    <td><.delivery_status pr={item.pr} ci={item.ci} /></td>
                    <td><.test_status test={item[:test]} /></td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>

        <section class="section-card section-card-acceptance">
          <div class="section-header">
            <div>
              <p class="section-kicker">Owner action</p>
              <h2 class="section-title">Ready for Acceptance</h2>
              <p class="section-copy">Merged work with CI, exact TEST SHA, and a direct environment link.</p>
            </div>
          </div>

          <%= if @payload.owner_view.ready_for_acceptance == [] do %>
            <p class="empty-state"><%= inventory_empty_copy(@payload.owner_view, "Nothing is waiting for acceptance.") %></p>
          <% else %>
            <div class="table-wrap">
              <table class="data-table owner-table acceptance-table">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>PR</th>
                    <th>CI</th>
                    <th>TEST deploy SHA</th>
                    <th>Environment</th>
                    <th :if={control_snapshot?(@payload)}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={item <- @payload.owner_view.ready_for_acceptance}>
                    <td><.owner_issue item={item} /></td>
                    <td><.pr_link pr={item.pr} /></td>
                    <td><.ci_link ci={item.ci} /></td>
                    <td class="mono"><%= compact_sha(item.test && item.test.sha) %></td>
                    <td><.test_link test={item.test} /></td>
                    <td :if={control_snapshot?(@payload)}>
                      <div class="owner-actions">
                        <button
                          type="button"
                          class="control-button control-button-primary"
                          phx-click="accept"
                          phx-value-issue={item.number}
                        >Accept</button>
                        <form phx-submit="rework" class="rework-form">
                          <input type="hidden" name="issue" value={item.number} />
                          <input
                            type="text"
                            name="reason"
                            class="rework-input"
                            placeholder="Short reason"
                            aria-label={"Rework reason for #{item.issue_identifier}"}
                            required
                          />
                          <button type="submit" class="control-button">Rework</button>
                        </form>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>

        <section class="section-card">
          <div class="section-header">
            <div>
              <p class="section-kicker">Next up</p>
              <h2 class="section-title">Backlog</h2>
              <p class="section-copy">A short preview; the total count stays visible in the status strip.</p>
            </div>
          </div>

          <%= if @payload.owner_view.backlog == [] do %>
            <p class="empty-state"><%= inventory_empty_copy(@payload.owner_view, "The backlog is empty.") %></p>
          <% else %>
            <div class="backlog-list">
              <article :for={item <- @payload.owner_view.backlog} class="backlog-item">
                <.owner_issue item={item} />
                <div class="backlog-actions">
                  <span class="routing-hint"><%= backlog_routing_label(item) %></span>
                  <span class="state-badge state-badge-warning"><%= item.stage || "Backlog" %></span>
                  <button
                    :if={control_snapshot?(@payload)}
                    type="button"
                    class="control-button control-button-primary"
                    phx-click="run"
                    phx-value-issue={item.number}
                  >Start</button>
                </div>
              </article>
            </div>
          <% end %>
        </section>

        <section class="section-card diagnostics-card">
          <details class="runtime-details">
            <summary>Runtime diagnostics</summary>
            <div class="diagnostics-grid">
              <div>
                <p class="metric-label">Total tokens</p>
                <p class="diagnostic-value numeric"><%= format_int(@payload.codex_totals.total_tokens) %></p>
                <p class="metric-detail numeric">
                  In <%= format_int(@payload.codex_totals.input_tokens) %> / Out <%= format_int(@payload.codex_totals.output_tokens) %>
                </p>
              </div>
              <div>
                <p class="metric-label">Runtime</p>
                <p class="diagnostic-value numeric"><%= format_runtime_seconds(total_runtime_seconds(@payload, @now)) %></p>
                <p class="metric-detail">Completed and active sessions.</p>
              </div>
              <div>
                <p class="metric-label">Model routing</p>
                <p :for={tier <- [:luna, :terra, :sol]} class="metric-detail numeric">
                  <%= model_diagnostic_label(@payload, tier) %>
                </p>
              </div>
              <div>
                <p class="metric-label">Rate limits</p>
                <pre class="code-panel diagnostics-code"><%= pretty_value(@payload.rate_limits) %></pre>
              </div>
            </div>

        <section class="runtime-section">
          <div class="section-header">
            <div>
              <h2 class="section-title">Running sessions</h2>
              <p class="section-copy">Active issues, last known agent activity, and token usage.</p>
            </div>
          </div>

          <%= if @payload.running == [] do %>
            <p class="empty-state">No active sessions.</p>
          <% else %>
            <div class="table-wrap">
              <table class="data-table data-table-running">
                <colgroup>
                  <col style="width: 12rem;" />
                  <col style="width: 8rem;" />
                  <col style="width: 7.5rem;" />
                  <col style="width: 8.5rem;" />
                  <col />
                  <col style="width: 10rem;" />
                </colgroup>
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>State</th>
                    <th>Model</th>
                    <th>Session</th>
                    <th>Runtime / turns</th>
                    <th>Codex update</th>
                    <th>Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={entry <- @payload.running}>
                    <td>
                      <div class="issue-stack">
                        <.issue_identifier identifier={entry.issue_identifier} url={entry.issue_url} />
                        <a class="issue-link" href={"/api/v1/#{entry.issue_identifier}"}>JSON details</a>
                      </div>
                    </td>
                    <td>
                      <span class={state_badge_class(entry.state)}>
                        <%= entry.state %>
                      </span>
                    </td>
                    <td><.model_status model={entry[:model]} compact /></td>
                    <td>
                      <div class="session-stack">
                        <%= if entry.session_id do %>
                          <button
                            type="button"
                            class="subtle-button"
                            data-label="Copy ID"
                            data-copy={entry.session_id}
                            onclick="navigator.clipboard.writeText(this.dataset.copy); this.textContent = 'Copied'; clearTimeout(this._copyTimer); this._copyTimer = setTimeout(() => { this.textContent = this.dataset.label }, 1200);"
                          >
                            Copy ID
                          </button>
                        <% else %>
                          <span class="muted">n/a</span>
                        <% end %>
                      </div>
                    </td>
                    <td class="numeric"><%= format_runtime_and_turns(entry.started_at, entry.turn_count, @now) %></td>
                    <td>
                      <div class="detail-stack">
                        <span
                          class="event-text"
                          title={entry.last_message || to_string(entry.last_event || "n/a")}
                        ><%= entry.last_message || to_string(entry.last_event || "n/a") %></span>
                        <span class="muted event-meta">
                          <%= entry.last_event || "n/a" %>
                          <%= if entry.last_event_at do %>
                            · <span class="mono numeric"><%= entry.last_event_at %></span>
                          <% end %>
                        </span>
                      </div>
                    </td>
                    <td>
                      <div class="token-stack numeric">
                        <span>Total: <%= format_int(entry.tokens.total_tokens) %></span>
                        <span class="muted">In <%= format_int(entry.tokens.input_tokens) %> / Out <%= format_int(entry.tokens.output_tokens) %></span>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>

        <section class="runtime-section">
          <div class="section-header">
            <div>
              <h2 class="section-title">Blocked sessions</h2>
              <p class="section-copy">Issues paused because Codex requested operator input or approval.</p>
            </div>
          </div>

          <%= if @payload.blocked == [] do %>
            <p class="empty-state">No blocked sessions.</p>
          <% else %>
            <div class="table-wrap">
              <table class="data-table" style="min-width: 760px;">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>State</th>
                    <th>Model</th>
                    <th>Session</th>
                    <th>Blocked at</th>
                    <th>Last update</th>
                    <th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={entry <- @payload.blocked}>
                    <td>
                      <div class="issue-stack">
                        <.issue_identifier identifier={entry.issue_identifier} url={entry.issue_url} />
                        <a class="issue-link" href={"/api/v1/#{entry.issue_identifier}"}>JSON details</a>
                      </div>
                    </td>
                    <td>
                      <span class={state_badge_class(entry.state || "Blocked")}>
                        <%= entry.state || "Blocked" %>
                      </span>
                    </td>
                    <td><.model_status model={entry[:model]} compact /></td>
                    <td>
                      <%= if entry.session_id do %>
                        <button
                          type="button"
                          class="subtle-button"
                          data-label="Copy ID"
                          data-copy={entry.session_id}
                          onclick="navigator.clipboard.writeText(this.dataset.copy); this.textContent = 'Copied'; clearTimeout(this._copyTimer); this._copyTimer = setTimeout(() => { this.textContent = this.dataset.label }, 1200);"
                        >
                          Copy ID
                        </button>
                      <% else %>
                        <span class="muted">n/a</span>
                      <% end %>
                    </td>
                    <td class="mono"><%= entry.blocked_at || "n/a" %></td>
                    <td>
                      <div class="detail-stack">
                        <span
                          class="event-text"
                          title={entry.last_message || to_string(entry.last_event || "n/a")}
                        ><%= entry.last_message || to_string(entry.last_event || "n/a") %></span>
                        <span class="muted event-meta">
                          <%= entry.last_event || "n/a" %>
                          <%= if entry.last_event_at do %>
                            · <span class="mono numeric"><%= entry.last_event_at %></span>
                          <% end %>
                        </span>
                      </div>
                    </td>
                    <td><%= entry.error || "n/a" %></td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>

        <section class="runtime-section">
          <div class="section-header">
            <div>
              <h2 class="section-title">Retry queue</h2>
              <p class="section-copy">Issues waiting for the next retry window.</p>
            </div>
          </div>

          <%= if @payload.retrying == [] do %>
            <p class="empty-state">No issues are currently backing off.</p>
          <% else %>
            <div class="table-wrap">
              <table class="data-table" style="min-width: 680px;">
                <thead>
                  <tr>
                    <th>Issue</th>
                    <th>Attempt</th>
                    <th>Model</th>
                    <th>Due at</th>
                    <th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={entry <- @payload.retrying}>
                    <td>
                      <div class="issue-stack">
                        <.issue_identifier identifier={entry.issue_identifier} url={entry.issue_url} />
                        <a class="issue-link" href={"/api/v1/#{entry.issue_identifier}"}>JSON details</a>
                      </div>
                    </td>
                    <td><%= entry.attempt %></td>
                    <td><.model_status model={entry[:model]} compact /></td>
                    <td class="mono"><%= entry.due_at || "n/a" %></td>
                    <td><%= entry.error || "n/a" %></td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>
          </details>
        </section>
      <% end %>
    </section>
    """
  end

  defp load_payload do
    case owner_control_client().snapshot() do
      {:ok, payload} ->
        payload

      :disabled ->
        Presenter.state_payload(orchestrator(), snapshot_timeout_ms())

      {:error, reason} ->
        orchestrator()
        |> Presenter.state_payload(snapshot_timeout_ms())
        |> Map.put(:control_error, inspect(reason))
    end
  end

  defp owner_control_client do
    Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient)
  end

  defp control_action(socket, action, params) do
    case owner_control_client().action(action, params) do
      {:ok, _result} ->
        {:noreply,
         socket
         |> put_flash(:info, control_success_message(action))
         |> assign(:payload, load_payload())}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, "Owner action rejected: #{format_control_error(reason)}")}
    end
  end

  defp orchestrator do
    Endpoint.config(:orchestrator) || SymphonyElixir.Orchestrator
  end

  defp snapshot_timeout_ms do
    Endpoint.config(:snapshot_timeout_ms) || 15_000
  end

  attr(:item, :map, required: true)

  defp owner_issue(assigns) do
    ~H"""
    <div class="owner-issue">
      <.issue_identifier identifier={@item.issue_identifier || "Unknown"} url={@item.issue_url} />
      <span :if={@item.title} class="owner-issue-title"><%= @item.title %></span>
    </div>
    """
  end

  attr(:model, :any, default: nil)
  attr(:compact, :boolean, default: false)

  defp model_status(assigns) do
    assigns =
      assigns
      |> assign(:tier, model_field(assigns.model, :selected_tier))
      |> assign(:actual_model, model_field(assigns.model, :actual_model))
      |> assign(:routing_reason, model_field(assigns.model, :routing_reason))
      |> assign(:escalation, model_escalation_label(assigns.model))

    ~H"""
    <%= if @tier do %>
      <div class="model-stack" title={@routing_reason}>
        <span class={model_badge_class(@tier)}><%= model_title(@tier) %></span>
        <span :if={!@compact && @actual_model} class="model-id mono"><%= @actual_model %></span>
        <span :if={@escalation} class="model-escalation"><%= @escalation %></span>
        <span :if={!@compact && @routing_reason} class="model-reason"><%= routing_reason_label(@routing_reason) %></span>
      </div>
    <% else %>
      <span class="muted">Auto on start</span>
    <% end %>
    """
  end

  attr(:pr, :any, default: nil)
  attr(:ci, :any, default: nil)

  defp delivery_status(assigns) do
    ~H"""
    <div class="delivery-stack">
      <.pr_link pr={@pr} />
      <.ci_link ci={@ci} />
    </div>
    """
  end

  attr(:pr, :any, default: nil)

  defp pr_link(assigns) do
    assigns = assign(assigns, :href, external_issue_url(assigns.pr && assigns.pr.url))

    ~H"""
    <%= if @pr && @pr.number do %>
      <%= if @href do %>
        <a class="delivery-link" href={@href} target="_blank" rel="noopener noreferrer">PR #<%= @pr.number %></a>
      <% else %>
        <span>PR #<%= @pr.number %></span>
      <% end %>
    <% else %>
      <span class="muted">No PR yet</span>
    <% end %>
    """
  end

  attr(:ci, :any, default: nil)

  defp ci_link(assigns) do
    assigns = assign(assigns, :href, external_issue_url(assigns.ci && assigns.ci.url))

    ~H"""
    <%= if @ci do %>
      <%= if @href do %>
        <a class={ci_badge_class(@ci.status)} href={@href} target="_blank" rel="noopener noreferrer"><%= ci_label(@ci.status) %></a>
      <% else %>
        <span class={ci_badge_class(@ci.status)}><%= ci_label(@ci.status) %></span>
      <% end %>
    <% else %>
      <span class="muted">CI not reported</span>
    <% end %>
    """
  end

  attr(:test, :any, default: nil)

  defp test_status(assigns) do
    ~H"""
    <div class="delivery-stack">
      <span :if={@test && @test.sha} class="mono"><%= compact_sha(@test.sha) %></span>
      <.test_link test={@test} />
    </div>
    """
  end

  attr(:test, :any, default: nil)

  defp test_link(assigns) do
    assigns = assign(assigns, :href, external_issue_url(assigns.test && assigns.test.url))

    ~H"""
    <%= if @href do %>
      <a class="test-link" href={@href} target="_blank" rel="noopener noreferrer">Open TEST</a>
    <% else %>
      <span class="muted">TEST not reported</span>
    <% end %>
    """
  end

  attr(:identifier, :string, required: true)
  attr(:url, :string, default: nil)

  defp issue_identifier(assigns) do
    assigns = assign(assigns, :href, external_issue_url(assigns.url))

    ~H"""
    <%= if @href do %>
      <a
        class="issue-id issue-id-link"
        href={@href}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={"Open #{@identifier} in the issue tracker"}
      ><%= @identifier %></a>
    <% else %>
      <span class="issue-id"><%= @identifier %></span>
    <% end %>
    """
  end

  defp external_issue_url(url) when is_binary(url) do
    url = String.trim(url)

    case URI.parse(url) do
      %URI{scheme: scheme, host: host}
      when scheme in ["http", "https"] and is_binary(host) and host != "" ->
        url

      _ ->
        nil
    end
  end

  defp external_issue_url(_url), do: nil

  defp total_runtime_seconds(payload, now) do
    (payload.codex_totals.seconds_running || 0) +
      Enum.reduce(payload.running, 0, fn entry, total ->
        total + runtime_seconds_from_started_at(entry.started_at, now)
      end)
  end

  defp format_count(value) when is_integer(value), do: format_int(value)
  defp format_count(_value), do: "—"

  defp format_elapsed(nil, _now), do: "—"
  defp format_elapsed(started_at, now), do: started_at |> runtime_seconds_from_started_at(now) |> format_runtime_seconds()

  defp compact_sha(sha) when is_binary(sha) and byte_size(sha) >= 8, do: String.slice(sha, 0, 8)
  defp compact_sha(_sha), do: "—"

  defp ci_label(status) do
    case status |> to_string() |> String.downcase() do
      value when value in ["success", "passed", "green"] -> "CI passed"
      value when value in ["failure", "failed", "error"] -> "CI failed"
      value when value in ["pending", "queued", "in_progress"] -> "CI pending"
      _ -> "CI unknown"
    end
  end

  defp ci_badge_class(status) do
    case ci_label(status) do
      "CI passed" -> "delivery-badge delivery-badge-success"
      "CI failed" -> "delivery-badge delivery-badge-danger"
      _ -> "delivery-badge delivery-badge-warning"
    end
  end

  defp attention_card_class([]), do: "section-card attention-card attention-card-clear"
  defp attention_card_class(_blocked), do: "section-card attention-card"

  defp inventory_empty_copy(%{available: false}, _empty_copy),
    do: "Project inventory is not reported by the current runtime snapshot."

  defp inventory_empty_copy(_owner_view, empty_copy), do: empty_copy

  defp control_snapshot?(%{version: 1, service: %{}, intake: %{}, workers: %{}}), do: true
  defp control_snapshot?(_payload), do: false

  defp service_live?(payload), do: get_in(payload, [:service, :live]) == true
  defp intake_active?(payload), do: get_in(payload, [:intake, :active]) == true

  defp workers_label(payload) do
    "#{get_in(payload, [:workers, :running]) || 0}/#{get_in(payload, [:workers, :limit]) || 0}"
  end

  defp model_active_label(payload, tier) do
    "#{model_title(tier)} #{model_count(payload, tier, :active)}"
  end

  defp model_diagnostic_label(payload, tier) do
    "#{model_title(tier)} #{model_count(payload, tier, :active)} active · #{model_count(payload, tier, :completed)} done"
  end

  defp model_count(payload, tier, key) do
    payload
    |> Map.get(:models, %{})
    |> model_field(tier)
    |> model_field(key)
    |> case do
      count when is_integer(count) and count >= 0 -> count
      _ -> 0
    end
  end

  defp model_field(%{} = model, key) when is_atom(key),
    do: Map.get(model, key) || Map.get(model, Atom.to_string(key))

  defp model_field(_model, _key), do: nil

  defp model_title(value) do
    value
    |> to_string()
    |> String.capitalize()
  end

  defp model_badge_class(tier), do: "model-badge model-badge-#{tier |> to_string() |> String.downcase()}"

  defp model_escalation_label(model) do
    case {model_field(model, :escalated_from), model_field(model, :selected_tier)} do
      {from, to} when not is_nil(from) and not is_nil(to) -> "#{model_title(from)} → #{model_title(to)}"
      _ -> nil
    end
  end

  defp routing_reason_label(reason) do
    reason
    |> to_string()
    |> String.replace([":", "_"], " ")
  end

  defp backlog_routing_label(item) do
    item
    |> Map.get(:labels, [])
    |> Enum.find_value("Auto on start", fn label ->
      case String.split(to_string(label), ":", parts: 2) do
        ["model", tier] when tier in ["luna", "terra", "sol"] -> "Override #{model_title(tier)}"
        _ -> nil
      end
    end)
  end

  defp service_badge_class(payload) do
    if service_live?(payload),
      do: "status-badge status-badge-live",
      else: "status-badge status-badge-offline"
  end

  defp intake_badge_class(payload) do
    if intake_active?(payload),
      do: "state-badge state-badge-active",
      else: "state-badge state-badge-warning"
  end

  defp release_status_class(payload) do
    if get_in(payload, [:test, :synced]),
      do: "release-status release-status-synced",
      else: "release-status release-status-drift"
  end

  defp parse_issue_number(issue) when is_integer(issue) and issue > 0, do: {:ok, issue}

  defp parse_issue_number(issue) when is_binary(issue) do
    case Integer.parse(String.trim_leading(String.trim(issue), "#")) do
      {number, ""} when number > 0 -> {:ok, number}
      _other -> {:error, :invalid_issue_number}
    end
  end

  defp parse_issue_number(_issue), do: {:error, :invalid_issue_number}

  defp control_success_message(:pause), do: "Intake paused"
  defp control_success_message(:resume), do: "Intake resumed"
  defp control_success_message(:restart), do: "Service restart accepted"
  defp control_success_message(:run), do: "Issue queued for Symphony"
  defp control_success_message(:accept), do: "Issue accepted"
  defp control_success_message(:rework), do: "Issue returned for rework"

  defp format_control_error({:owner_control_http_error, status}), do: "control HTTP #{status}"
  defp format_control_error({:owner_control_action_rejected, message}), do: message
  defp format_control_error(:unsupported_action), do: "unsupported action"
  defp format_control_error(_reason), do: "control service unavailable"

  defp format_runtime_and_turns(started_at, turn_count, now) when is_integer(turn_count) and turn_count > 0 do
    "#{format_runtime_seconds(runtime_seconds_from_started_at(started_at, now))} / #{turn_count}"
  end

  defp format_runtime_and_turns(started_at, _turn_count, now),
    do: format_runtime_seconds(runtime_seconds_from_started_at(started_at, now))

  defp format_runtime_seconds(seconds) when is_number(seconds) do
    whole_seconds = max(trunc(seconds), 0)
    mins = div(whole_seconds, 60)
    secs = rem(whole_seconds, 60)
    "#{mins}m #{secs}s"
  end

  defp runtime_seconds_from_started_at(%DateTime{} = started_at, %DateTime{} = now) do
    DateTime.diff(now, started_at, :second)
  end

  defp runtime_seconds_from_started_at(started_at, %DateTime{} = now) when is_binary(started_at) do
    case DateTime.from_iso8601(started_at) do
      {:ok, parsed, _offset} -> runtime_seconds_from_started_at(parsed, now)
      _ -> 0
    end
  end

  defp runtime_seconds_from_started_at(_started_at, _now), do: 0

  defp format_int(value) when is_integer(value) do
    value
    |> Integer.to_string()
    |> String.reverse()
    |> String.replace(~r/.{3}(?=.)/, "\\0,")
    |> String.reverse()
  end

  defp format_int(_value), do: "n/a"

  defp snapshot_freshness(payload) do
    case Map.get(payload, :owner_view) do
      %{updated_at: updated_at} when is_binary(updated_at) -> updated_at
      _ -> payload.generated_at
    end
  end

  defp state_badge_class(state) do
    base = "state-badge"
    normalized = state |> to_string() |> String.downcase()

    cond do
      String.contains?(normalized, ["progress", "running", "active"]) -> "#{base} state-badge-active"
      String.contains?(normalized, ["blocked", "error", "failed"]) -> "#{base} state-badge-danger"
      String.contains?(normalized, ["todo", "queued", "pending", "retry"]) -> "#{base} state-badge-warning"
      true -> base
    end
  end

  defp schedule_runtime_tick do
    Process.send_after(self(), :runtime_tick, @runtime_tick_ms)
  end

  defp schedule_control_refresh do
    Process.send_after(self(), :control_refresh, @control_refresh_ms)
  end

  defp pretty_value(nil), do: "n/a"
  defp pretty_value(value), do: inspect(value, pretty: true, limit: :infinity)
end
