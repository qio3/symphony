defmodule SymphonyElixirWeb.DashboardLive do
  @moduledoc """
  Live observability dashboard for Symphony.
  """

  use Phoenix.LiveView, layout: {SymphonyElixirWeb.Layouts, :app}

  alias SymphonyElixirWeb.{Endpoint, ObservabilityPubSub, Presenter}
  @runtime_tick_ms 1_000

  @impl true
  def mount(_params, _session, socket) do
    socket =
      socket
      |> assign(:payload, load_payload())
      |> assign(:now, DateTime.utc_now())

    if connected?(socket) do
      :ok = ObservabilityPubSub.subscribe()
      schedule_runtime_tick()
    end

    {:ok, socket}
  end

  @impl true
  def handle_info(:runtime_tick, socket) do
    schedule_runtime_tick()
    {:noreply, assign(socket, :now, DateTime.utc_now())}
  end

  @impl true
  def handle_info(:observability_updated, socket) do
    {:noreply,
     socket
     |> assign(:payload, load_payload())
     |> assign(:now, DateTime.utc_now())}
  end

  @impl true
  def render(assigns) do
    ~H"""
    <section class="dashboard-shell">
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
            <span class="status-badge status-badge-live">
              <span class="status-badge-dot"></span>
              Live
            </span>
            <span class="status-badge status-badge-offline">
              <span class="status-badge-dot"></span>
              Offline
            </span>
            <span class="freshness-label mono">
              Updated <%= snapshot_freshness(@payload) %>
            </span>
          </div>
        </div>
      </header>

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
            <p class="metric-detail">Ready for Symphony.</p>
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
                    <th>In work</th>
                    <th>PR / CI</th>
                    <th>TEST</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={item <- @payload.owner_view.work_items}>
                    <td><.owner_issue item={item} /></td>
                    <td><span class={state_badge_class(item.status || item.stage)}><%= item.stage || "Unknown" %></span></td>
                    <td class="numeric"><%= format_elapsed(item.started_at, @now) %></td>
                    <td><.delivery_status pr={item.pr} ci={item.ci} /></td>
                    <td><.test_status test={item.test} /></td>
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
                  </tr>
                </thead>
                <tbody>
                  <tr :for={item <- @payload.owner_view.ready_for_acceptance}>
                    <td><.owner_issue item={item} /></td>
                    <td><.pr_link pr={item.pr} /></td>
                    <td><.ci_link ci={item.ci} /></td>
                    <td class="mono"><%= compact_sha(item.test && item.test.sha) %></td>
                    <td><.test_link test={item.test} /></td>
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
                <span class="state-badge state-badge-warning"><%= item.stage || "Backlog" %></span>
              </article>
            </div>
          <% end %>
        </section>

        <section class="section-card">
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

        <section class="section-card">
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

        <section class="section-card">
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
                    <td class="mono"><%= entry.due_at || "n/a" %></td>
                    <td><%= entry.error || "n/a" %></td>
                  </tr>
                </tbody>
              </table>
            </div>
          <% end %>
        </section>

        <section class="section-card diagnostics-card">
          <details>
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
                <p class="metric-label">Rate limits</p>
                <pre class="code-panel diagnostics-code"><%= pretty_value(@payload.rate_limits) %></pre>
              </div>
            </div>
          </details>
        </section>
      <% end %>
    </section>
    """
  end

  defp load_payload do
    Presenter.state_payload(orchestrator(), snapshot_timeout_ms())
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

  defp pretty_value(nil), do: "n/a"
  defp pretty_value(value), do: inspect(value, pretty: true, limit: :infinity)
end
