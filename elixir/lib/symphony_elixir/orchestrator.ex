defmodule SymphonyElixir.Orchestrator do
  @moduledoc """
  Polls the configured issue tracker and dispatches repository copies to Codex-backed workers.
  """

  use GenServer
  require Logger
  import Bitwise, only: [<<<: 2]

  alias SymphonyElixir.{
    AgentRunner,
    Config,
    ModelRouter,
    SourceCircuit,
    StatusDashboard,
    Tracker,
    UsageCost,
    UsageLedger,
    Workspace
  }

  alias SymphonyElixir.Codex.AppServer
  alias SymphonyElixir.OwnerControl.Client, as: OwnerControlClient
  alias SymphonyElixir.Tracker.Issue

  @continuation_retry_delay_ms 1_000
  @capacity_retry_delay_ms 5_000
  @paused_retry_delay_ms 30_000
  @failure_retry_base_ms 10_000
  @before_run_hook_output_max_bytes 512
  @before_run_hook_output_truncation "... [truncated]"
  # Slightly above the dashboard render interval so "checking now…" can render.
  @poll_transition_render_delay_ms 20
  @account_rate_limit_refresh_ms 300_000
  @empty_codex_totals %{
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    seconds_running: 0
  }

  defmodule State do
    @moduledoc """
    Runtime state for the orchestrator polling loop.
    """

    defstruct [
      :poll_interval_ms,
      :max_concurrent_agents,
      :next_poll_due_at_ms,
      :poll_check_in_progress,
      :tick_timer_ref,
      :tick_token,
      account_rate_limits_reader: &AppServer.read_rate_limits/0,
      account_rate_limit_refresh_in_flight: false,
      task_supervisor: SymphonyElixir.TaskSupervisor,
      running: %{},
      completed: MapSet.new(),
      claimed: MapSet.new(),
      blocked: %{},
      retry_attempts: %{},
      source_circuit: SourceCircuit.new(),
      model_completed_counts: %{luna: 0, terra: 0, sol: 0},
      codex_totals: nil,
      codex_rate_limits: nil,
      weekly_quota_observation: nil,
      usage_ledger: nil
    ]
  end

  @doc false
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    name = Keyword.get(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @impl true
  def init(opts) do
    case Config.loaded_settings_snapshot() do
      {:ok, %{settings: config, workflow_path: workflow_path}} ->
        now_ms = System.monotonic_time(:millisecond)

        usage_ledger_path = Keyword.get(opts, :usage_ledger_path, usage_ledger_path(config))

        with {:ok, usage_ledger} <- UsageLedger.load(usage_ledger_path) do
          state = %State{
            poll_interval_ms: config.polling.interval_ms,
            max_concurrent_agents: config.agent.max_concurrent_agents,
            next_poll_due_at_ms: now_ms,
            poll_check_in_progress: false,
            tick_timer_ref: nil,
            tick_token: nil,
            account_rate_limits_reader: Keyword.get(opts, :account_rate_limits_reader, &AppServer.read_rate_limits/0),
            account_rate_limit_refresh_in_flight: false,
            task_supervisor: Keyword.get(opts, :task_supervisor, SymphonyElixir.TaskSupervisor),
            codex_totals: @empty_codex_totals,
            codex_rate_limits: nil,
            usage_ledger: usage_ledger
          }

          start_terminal_workspace_cleanup(state, startup_cleanup_config(config, workflow_path))
          state = schedule_tick(state, 0)
          send(self(), :refresh_account_rate_limits)

          {:ok, state}
        end

      {:error, reason} ->
        {:stop, reason}
    end
  end

  @impl true
  def handle_info({:tick, tick_token}, %{tick_token: tick_token} = state)
      when is_reference(tick_token) do
    state = refresh_runtime_config(state)

    state = %{
      state
      | poll_check_in_progress: true,
        next_poll_due_at_ms: nil,
        tick_timer_ref: nil,
        tick_token: nil
    }

    notify_dashboard()
    :ok = schedule_poll_cycle_start()
    {:noreply, state}
  end

  def handle_info({:tick, _tick_token}, state), do: {:noreply, state}

  def handle_info(:tick, state) do
    state = refresh_runtime_config(state)

    state = %{
      state
      | poll_check_in_progress: true,
        next_poll_due_at_ms: nil,
        tick_timer_ref: nil,
        tick_token: nil
    }

    notify_dashboard()
    :ok = schedule_poll_cycle_start()
    {:noreply, state}
  end

  def handle_info(:run_poll_cycle, state) do
    state = refresh_runtime_config(state)
    state = maybe_dispatch(state)
    state = schedule_tick(state, state.poll_interval_ms)
    state = %{state | poll_check_in_progress: false}

    notify_dashboard()
    {:noreply, state}
  end

  def handle_info(:refresh_account_rate_limits, state) do
    Process.send_after(self(), :refresh_account_rate_limits, @account_rate_limit_refresh_ms)
    {:noreply, start_account_rate_limit_refresh(state)}
  end

  def handle_info({:account_rate_limits_result, {:ok, rate_limits}}, state) when is_map(rate_limits) do
    state =
      state
      |> Map.put(:account_rate_limit_refresh_in_flight, false)
      |> apply_codex_rate_limits(%{
        event: :rate_limits_snapshot,
        payload: rate_limits,
        timestamp: DateTime.utc_now()
      })

    notify_dashboard()
    {:noreply, state}
  end

  def handle_info({:account_rate_limits_result, {:error, reason}}, state) do
    Logger.debug("Codex account rate-limit refresh unavailable: #{inspect(reason)}")
    {:noreply, %{state | account_rate_limit_refresh_in_flight: false}}
  end

  def handle_info({:account_rate_limits_result, unexpected}, state) do
    Logger.warning("Codex account rate-limit refresh returned an invalid result: #{inspect(unexpected)}")
    {:noreply, %{state | account_rate_limit_refresh_in_flight: false}}
  end

  def handle_info(
        {:DOWN, ref, :process, _pid, reason},
        %{running: running} = state
      ) do
    case find_issue_id_for_ref(running, ref) do
      nil ->
        {:noreply, state}

      issue_id ->
        {running_entry, state} = pop_running_entry(state, issue_id)
        state = record_usage_completion(state, issue_id, running_entry)
        state = record_session_completion_totals(state, running_entry)
        session_id = running_entry_session_id(running_entry)

        state = handle_agent_down(reason, state, issue_id, running_entry, session_id)

        Logger.info("Agent task finished for issue_id=#{issue_id} session_id=#{session_id} reason=#{inspect(reason)}")

        notify_dashboard()
        {:noreply, state}
    end
  end

  def handle_info({:worker_runtime_info, issue_id, runtime_info}, %{running: running} = state)
      when is_binary(issue_id) and is_map(runtime_info) do
    case Map.get(running, issue_id) do
      nil ->
        {:noreply, state}

      running_entry ->
        updated_running_entry =
          running_entry
          |> maybe_put_runtime_value(:worker_host, runtime_info[:worker_host])
          |> maybe_put_runtime_value(:workspace_path, runtime_info[:workspace_path])

        notify_dashboard()
        {:noreply, %{state | running: Map.put(running, issue_id, updated_running_entry)}}
    end
  end

  def handle_info({:worker_routing_info, issue_id, routing_info}, %{running: running} = state)
      when is_binary(issue_id) and is_map(routing_info) do
    case Map.get(running, issue_id) do
      nil ->
        {:noreply, state}

      running_entry ->
        normalized_routing_info =
          case Map.fetch(routing_info, :selected_tier) do
            {:ok, tier} -> Map.put_new(routing_info, :selected_model_tier, tier)
            :error -> routing_info
          end

        updated_running_entry = Map.merge(running_entry, normalized_routing_info)
        notify_dashboard()
        {:noreply, %{state | running: Map.put(running, issue_id, updated_running_entry)}}
    end
  end

  def handle_info(
        {:codex_worker_update, issue_id, %{event: _, timestamp: _} = update},
        %{running: running} = state
      ) do
    case Map.get(running, issue_id) do
      nil ->
        {:noreply, state}

      running_entry ->
        {updated_running_entry, token_delta} = integrate_codex_update(running_entry, update)

        state =
          state
          |> apply_codex_token_delta(token_delta)
          |> apply_codex_rate_limits(update)
          |> record_usage_sample(issue_id, updated_running_entry, update)

        notify_dashboard()
        {:noreply, %{state | running: Map.put(running, issue_id, updated_running_entry)}}
    end
  end

  def handle_info({:codex_worker_update, _issue_id, _update}, state), do: {:noreply, state}

  def handle_info({:retry_issue, issue_id, retry_token}, state) do
    result =
      case pop_retry_attempt_state(state, issue_id, retry_token) do
        {:ok, attempt, metadata, state} -> handle_retry_issue(state, issue_id, attempt, metadata)
        :missing -> {:noreply, state}
      end

    notify_dashboard()
    result
  end

  def handle_info({:retry_issue, _issue_id}, state), do: {:noreply, state}

  def handle_info(msg, state) do
    Logger.debug("Orchestrator ignored message: #{inspect(msg)}")
    {:noreply, state}
  end

  defp handle_agent_down({:model_exhausted, route, reason}, state, issue_id, running_entry, session_id) do
    if Map.get(route, :selected_tier) == :sol or ModelRouter.terminal_exhaustion?(route, reason) do
      quarantine_running_failure(
        state,
        issue_id,
        running_entry,
        "model ceiling exhausted on Sol: #{reason}"
      )
    else
      escalated_route = ModelRouter.maybe_escalate(route, reason)

      Logger.warning("Agent model exhausted for issue_id=#{issue_id} session_id=#{session_id} from=#{route.selected_tier} to=#{escalated_route.selected_tier} reason=#{reason}; scheduling retry")

      schedule_issue_retry(state, issue_id, next_retry_attempt_from_running(running_entry), %{
        identifier: running_entry.identifier,
        issue_url: running_entry.issue.url,
        error: "model exhausted: #{reason}",
        worker_host: Map.get(running_entry, :worker_host),
        workspace_path: Map.get(running_entry, :workspace_path),
        model_route: escalated_route,
        escalation_reason: reason
      })
    end
  end

  defp handle_agent_down(:normal, state, issue_id, running_entry, session_id) do
    if input_required_blocker?(running_entry) do
      block_input_required_agent_down(state, issue_id, running_entry, session_id, :normal)
    else
      Logger.info("Agent task completed for issue_id=#{issue_id} session_id=#{session_id}; scheduling active-state continuation check")

      state
      |> record_model_completion(running_entry)
      |> complete_issue(issue_id)
      |> schedule_issue_retry(issue_id, 1, %{
        identifier: running_entry.identifier,
        issue_url: running_entry.issue.url,
        delay_type: :continuation,
        worker_host: Map.get(running_entry, :worker_host),
        workspace_path: Map.get(running_entry, :workspace_path),
        model_route: Map.get(running_entry, :model_route)
      })
    end
  end

  defp handle_agent_down(
         {:workspace_hook_failed, "before_run", status, output},
         state,
         issue_id,
         running_entry,
         session_id
       )
       when is_integer(status) and is_binary(output) do
    error = "workspace before_run hook failed (exit #{status}): #{sanitize_before_run_hook_output(output)}"

    Logger.warning(
      "Agent task blocked after deterministic before_run hook failure for issue_id=#{issue_id} " <>
        "session_id=#{session_id}: #{error}"
    )

    case quarantine_before_run_hook_failure(running_entry, error) do
      :ok ->
        release_issue_claim(state, issue_id)

      {:error, reason} ->
        Logger.warning("Could not persist deterministic before_run quarantine for issue_id=#{issue_id}: #{inspect(reason)}")

        block_issue_from_entry(state, issue_id, running_entry, error)
    end
  end

  defp handle_agent_down(reason, state, issue_id, running_entry, session_id) do
    if input_required_blocker?(running_entry) do
      block_input_required_agent_down(state, issue_id, running_entry, session_id, reason)
    else
      retry_agent_down(state, issue_id, running_entry, session_id, reason)
    end
  end

  defp sanitize_before_run_hook_output(output) when is_binary(output) do
    output
    |> String.replace_invalid("�")
    |> String.replace(~r/[\p{Cc}]/u, " ")
    |> collapse_hook_output_whitespace()
    |> trim_ascii_spaces()
    |> truncate_before_run_hook_output()
  end

  defp quarantine_before_run_hook_failure(%{issue: %Issue{} = issue}, error) do
    with true <- owner_control_enabled?(),
         {:ok, issue_number} <- owner_control_issue_number(issue),
         client <- Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient),
         true <- function_exported?(client, :quarantine_before_run, 2),
         {:ok, _response} <- client.quarantine_before_run(issue_number, error) do
      :ok
    else
      false -> {:error, :owner_control_quarantine_unavailable}
      error -> {:error, error}
    end
  rescue
    exception -> {:error, {:owner_control_quarantine_failed, Exception.message(exception)}}
  catch
    kind, reason -> {:error, {:owner_control_quarantine_failed, {kind, reason}}}
  end

  defp quarantine_before_run_hook_failure(_running_entry, _error),
    do: {:error, :owner_control_issue_unavailable}

  defp collapse_hook_output_whitespace(output) do
    {chunks, _previous_space?} =
      for <<byte <- output>>, reduce: {[], true} do
        {chunks, previous_space?} when byte <= 32 or byte == 127 ->
          if previous_space?, do: {chunks, true}, else: {[chunks, " "], true}

        {chunks, _previous_space?} ->
          {[chunks, <<byte>>], false}
      end

    IO.iodata_to_binary(chunks)
  end

  defp trim_ascii_spaces(<<" ", rest::binary>>), do: trim_ascii_spaces(rest)

  defp trim_ascii_spaces(output) when byte_size(output) > 0 do
    last_index = byte_size(output) - 1

    if binary_part(output, last_index, 1) == " " do
      output
      |> binary_part(0, last_index)
      |> trim_ascii_spaces()
    else
      output
    end
  end

  defp trim_ascii_spaces(output), do: output

  defp truncate_before_run_hook_output(output)
       when byte_size(output) <= @before_run_hook_output_max_bytes,
       do: output

  defp truncate_before_run_hook_output(output) do
    prefix_bytes = @before_run_hook_output_max_bytes - byte_size(@before_run_hook_output_truncation)

    output
    |> utf8_prefix_within_bytes(prefix_bytes)
    |> Kernel.<>(@before_run_hook_output_truncation)
  end

  defp utf8_prefix_within_bytes(output, max_bytes) do
    {chunks, _used_bytes} =
      output
      |> String.graphemes()
      |> Enum.reduce_while({[], 0}, fn grapheme, {chunks, used_bytes} ->
        grapheme_bytes = byte_size(grapheme)

        if used_bytes + grapheme_bytes <= max_bytes do
          {:cont, {[chunks, grapheme], used_bytes + grapheme_bytes}}
        else
          {:halt, {chunks, used_bytes}}
        end
      end)

    IO.iodata_to_binary(chunks)
  end

  defp block_input_required_agent_down(state, issue_id, running_entry, session_id, reason) do
    error = blocker_error(running_entry, "agent exited: #{inspect(reason)}")

    Logger.warning("Agent task blocked for issue_id=#{issue_id} issue_identifier=#{running_entry.identifier} session_id=#{session_id}: #{error}")

    block_issue_from_entry(state, issue_id, running_entry, error)
  end

  defp retry_agent_down(state, issue_id, running_entry, session_id, reason) do
    Logger.warning("Agent task exited for issue_id=#{issue_id} session_id=#{session_id} reason=#{inspect(reason)}; scheduling retry")

    next_attempt = next_retry_attempt_from_running(running_entry)
    model_route = retry_model_route(running_entry, reason)

    if same_sol_retry?(running_entry, model_route) do
      quarantine_running_failure(
        state,
        issue_id,
        running_entry,
        "Sol attempt failed and cannot be retried as Sol: #{inspect(reason, limit: 20, printable_limit: 512)}"
      )
    else
      maybe_schedule_bounded_retry(
        state,
        issue_id,
        running_entry,
        reason,
        next_attempt,
        model_route
      )
    end
  end

  defp maybe_schedule_bounded_retry(state, issue_id, running_entry, reason, next_attempt, model_route) do
    if is_map(model_route) and ModelRouter.terminal_retry?(model_route) do
      quarantine_running_failure(
        state,
        issue_id,
        running_entry,
        "repeated failure reached retry limit: #{inspect(reason, limit: 20, printable_limit: 512)}"
      )
    else
      schedule_issue_retry(state, issue_id, next_attempt, %{
        identifier: running_entry.identifier,
        issue_url: running_entry.issue.url,
        error: "agent exited: #{inspect(reason)}",
        worker_host: Map.get(running_entry, :worker_host),
        workspace_path: Map.get(running_entry, :workspace_path),
        model_route: model_route,
        escalation_reason: repeated_failure_escalation_reason(running_entry, model_route)
      })
    end
  end

  defp same_sol_retry?(running_entry, retry_route) when is_map(retry_route) do
    get_in(running_entry, [:model_route, :selected_tier]) == :sol and
      Map.get(retry_route, :selected_tier) == :sol
  end

  defp same_sol_retry?(_running_entry, _retry_route), do: false

  defp quarantine_running_failure(state, issue_id, running_entry, reason) do
    Logger.error(
      "Agent task entered system quarantine for issue_id=#{issue_id} " <>
        "issue_identifier=#{running_entry.identifier}: #{reason}"
    )

    case quarantine_before_run_hook_failure(running_entry, reason) do
      :ok -> release_issue_claim(state, issue_id)
      {:error, _persistence_reason} -> block_issue_from_entry(state, issue_id, running_entry, reason)
    end
  end

  defp retry_model_route(running_entry, reason) do
    case Map.get(running_entry, :model_route) do
      %{} = route -> ModelRouter.retry_route(route, reason)
      _ -> nil
    end
  end

  defp repeated_failure_escalation_reason(running_entry, %{} = retry_route) do
    case Map.get(running_entry, :model_route) do
      %{selected_tier: previous_tier} when previous_tier != retry_route.selected_tier ->
        :repeated_root_cause

      _ ->
        nil
    end
  end

  defp repeated_failure_escalation_reason(_running_entry, _retry_route), do: nil

  @doc false
  @spec maybe_dispatch_for_test(term()) :: term()
  def maybe_dispatch_for_test(%State{} = state), do: maybe_dispatch(state)

  defp maybe_dispatch(%State{} = state) do
    if source_circuit_open?(state), do: state, else: do_maybe_dispatch(state)
  end

  defp do_maybe_dispatch(%State{} = state) do
    state = reconcile_running_issues(state)
    state = if source_circuit_open?(state), do: state, else: reconcile_blocked_issues(state)
    state = wake_paused_retries(state)

    with :ok <- Config.validate!(),
         {:ok, issues, state} <- fetch_dispatch_issues(state),
         true <- fresh_dispatch_slots_available?(state) do
      choose_issues(issues, state)
    else
      {:error, :missing_linear_api_token} ->
        Logger.error("Tracker API token missing in WORKFLOW.md")
        state

      {:error, :missing_linear_project_slug} ->
        Logger.error("Tracker project scope missing in WORKFLOW.md")
        state

      {:error, :missing_tracker_kind} ->
        Logger.error("Tracker kind missing in WORKFLOW.md")

        state

      {:error, {:unsupported_tracker_kind, kind}} ->
        Logger.error("Unsupported tracker kind in WORKFLOW.md: #{inspect(kind)}")

        state

      {:error, {:invalid_workflow_config, message}} ->
        Logger.error("Invalid WORKFLOW.md config: #{message}")
        state

      {:error, {:missing_workflow_file, path, reason}} ->
        Logger.error("Missing WORKFLOW.md at #{path}: #{inspect(reason)}")
        state

      {:error, :workflow_front_matter_not_a_map} ->
        Logger.error("Failed to parse WORKFLOW.md: workflow front matter must decode to a map")
        state

      {:error, {:workflow_parse_error, reason}} ->
        Logger.error("Failed to parse WORKFLOW.md: #{inspect(reason)}")
        state

      {:source_circuit_open, state} ->
        state

      {:source_error, state} ->
        state

      {:error, reason} ->
        Logger.error("Failed to fetch from issue tracker: #{inspect(reason)}")
        state

      false ->
        state
    end
  end

  defp fetch_dispatch_issues(state) do
    if source_circuit_open?(state) do
      {:source_circuit_open, state}
    else
      case Tracker.fetch_issues_by_states(Config.settings!().tracker.active_states) do
        {:ok, issues} ->
          {:ok, issues, source_circuit_success(state)}

        {:error, reason} ->
          Logger.error("Failed to fetch from issue tracker: #{inspect(reason)}")
          {:source_error, source_circuit_failure(state, reason)}
      end
    end
  end

  defp reconcile_running_issues(%State{} = state) do
    state = reconcile_stalled_running_issues(state)
    running_ids = Map.keys(state.running)

    if running_ids == [] do
      state
    else
      case Tracker.fetch_issues_by_ids(running_ids) do
        {:ok, issues} ->
          issues
          |> reconcile_running_issue_states(
            source_circuit_success(state),
            active_state_set(),
            terminal_state_set()
          )
          |> reconcile_missing_running_issue_ids(running_ids, issues)

        {:error, reason} ->
          Logger.debug("Failed to refresh running issue states: #{inspect(reason)}; keeping active workers")

          source_circuit_failure(state, reason)
      end
    end
  end

  defp reconcile_blocked_issues(%State{} = state) do
    blocked_ids = Map.keys(state.blocked)

    if blocked_ids == [] do
      state
    else
      case Tracker.fetch_issues_by_ids(blocked_ids) do
        {:ok, issues} ->
          issues
          |> reconcile_blocked_issue_states(
            source_circuit_success(state),
            active_state_set(),
            terminal_state_set()
          )
          |> reconcile_missing_blocked_issue_ids(blocked_ids, issues)

        {:error, reason} ->
          Logger.debug("Failed to refresh blocked issue states: #{inspect(reason)}; keeping blocked issues")

          source_circuit_failure(state, reason)
      end
    end
  end

  @doc false
  @spec reconcile_issue_states_for_test([Issue.t()], term()) :: term()
  def reconcile_issue_states_for_test(issues, %State{} = state) when is_list(issues) do
    reconcile_running_issue_states(issues, state, active_state_set(), terminal_state_set())
  end

  def reconcile_issue_states_for_test(issues, state) when is_list(issues) do
    reconcile_running_issue_states(issues, state, active_state_set(), terminal_state_set())
  end

  @doc false
  @spec reconcile_blocked_issue_states_for_test([Issue.t()], term()) :: term()
  def reconcile_blocked_issue_states_for_test(issues, %State{} = state) when is_list(issues) do
    reconcile_blocked_issue_states(issues, state, active_state_set(), terminal_state_set())
  end

  @doc false
  @spec handle_retry_issue_lookup_for_test(Issue.t(), term(), String.t(), non_neg_integer(), map()) ::
          term()
  def handle_retry_issue_lookup_for_test(%Issue{} = issue, %State{} = state, issue_id, attempt, metadata)
      when is_binary(issue_id) and is_integer(attempt) and attempt >= 0 and is_map(metadata) do
    {:noreply, updated_state} = handle_retry_issue_lookup(issue, state, issue_id, attempt, metadata)
    updated_state
  end

  @doc false
  @spec should_dispatch_issue_for_test(Issue.t(), term()) :: boolean()
  def should_dispatch_issue_for_test(%Issue{} = issue, %State{} = state) do
    should_dispatch_issue?(
      issue,
      state,
      active_state_set(),
      terminal_state_set(),
      owner_control_intake_active?(),
      :owner_control_disabled,
      :owner_control_disabled
    )
  end

  @doc false
  @spec retry_dispatch_allowed_for_test(Issue.t(), term()) :: boolean()
  def retry_dispatch_allowed_for_test(%Issue{} = issue, %State{} = state) do
    retry_dispatch_allowed?(true, issue, state, nil)
  end

  @doc false
  @spec revalidate_issue_for_dispatch_for_test(Issue.t(), ([String.t()] -> term())) ::
          {:ok, Issue.t()} | {:skip, Issue.t() | :missing} | {:error, term()}
  def revalidate_issue_for_dispatch_for_test(%Issue{} = issue, issue_fetcher)
      when is_function(issue_fetcher, 1) do
    revalidate_issue_for_dispatch(issue, issue_fetcher, terminal_state_set())
  end

  @doc false
  @spec sort_issues_for_dispatch_for_test([Issue.t()]) :: [Issue.t()]
  def sort_issues_for_dispatch_for_test(issues) when is_list(issues) do
    sort_issues_for_dispatch(issues)
  end

  @doc false
  @spec select_worker_host_for_test(term(), String.t() | nil) :: String.t() | nil | :no_worker_capacity
  def select_worker_host_for_test(%State{} = state, preferred_worker_host) do
    select_worker_host(state, preferred_worker_host)
  end

  defp reconcile_running_issue_states([], state, _active_states, _terminal_states), do: state

  defp reconcile_running_issue_states([issue | rest], state, active_states, terminal_states) do
    reconcile_running_issue_states(
      rest,
      reconcile_issue_state(issue, state, active_states, terminal_states),
      active_states,
      terminal_states
    )
  end

  defp reconcile_issue_state(%Issue{} = issue, state, active_states, terminal_states) do
    cond do
      terminal_issue_state?(issue.state, terminal_states) ->
        Logger.info("Issue moved to terminal state: #{issue_context(issue)} state=#{issue.state}; stopping active agent")

        terminate_running_issue(state, issue.id, true)

      !issue_routable?(issue) ->
        Logger.info("Issue no longer routed to this worker: #{issue_context(issue)} assignee=#{inspect(issue.assignee_id)}; stopping active agent")

        terminate_running_issue(state, issue.id, false)

      active_issue_state?(issue.state, active_states) ->
        refresh_running_issue_state(state, issue)

      true ->
        Logger.info("Issue moved to non-active state: #{issue_context(issue)} state=#{issue.state}; stopping active agent")

        terminate_running_issue(state, issue.id, false)
    end
  end

  defp reconcile_issue_state(_issue, state, _active_states, _terminal_states), do: state

  defp reconcile_blocked_issue_states([], state, _active_states, _terminal_states), do: state

  defp reconcile_blocked_issue_states([issue | rest], state, active_states, terminal_states) do
    reconcile_blocked_issue_states(
      rest,
      reconcile_blocked_issue_state(issue, state, active_states, terminal_states),
      active_states,
      terminal_states
    )
  end

  defp reconcile_blocked_issue_state(%Issue{} = issue, state, active_states, terminal_states) do
    cond do
      terminal_issue_state?(issue.state, terminal_states) ->
        Logger.info("Blocked issue moved to terminal state: #{issue_context(issue)} state=#{issue.state}; releasing block")
        cleanup_issue_workspace(issue, Map.get(state.blocked, issue.id, %{}))
        release_issue_claim(state, issue.id)

      !issue_routable?(issue) ->
        Logger.info("Blocked issue no longer routed to this worker: #{issue_context(issue)} assignee=#{inspect(issue.assignee_id)}; releasing block")
        release_issue_claim(state, issue.id)

      active_issue_state?(issue.state, active_states) ->
        refresh_blocked_issue_state(state, issue)

      true ->
        Logger.info("Blocked issue moved to non-active state: #{issue_context(issue)} state=#{issue.state}; releasing block")
        release_issue_claim(state, issue.id)
    end
  end

  defp reconcile_blocked_issue_state(_issue, state, _active_states, _terminal_states), do: state

  defp reconcile_missing_running_issue_ids(%State{} = state, requested_issue_ids, issues)
       when is_list(requested_issue_ids) and is_list(issues) do
    visible_issue_ids =
      issues
      |> Enum.flat_map(fn
        %Issue{id: issue_id} when is_binary(issue_id) -> [issue_id]
        _ -> []
      end)
      |> MapSet.new()

    Enum.reduce(requested_issue_ids, state, fn issue_id, state_acc ->
      if MapSet.member?(visible_issue_ids, issue_id) do
        state_acc
      else
        log_missing_running_issue(state_acc, issue_id)
        terminate_running_issue(state_acc, issue_id, false)
      end
    end)
  end

  defp reconcile_missing_running_issue_ids(state, _requested_issue_ids, _issues), do: state

  defp reconcile_missing_blocked_issue_ids(%State{} = state, requested_issue_ids, issues)
       when is_list(requested_issue_ids) and is_list(issues) do
    visible_issue_ids =
      issues
      |> Enum.flat_map(fn
        %Issue{id: issue_id} when is_binary(issue_id) -> [issue_id]
        _ -> []
      end)
      |> MapSet.new()

    Enum.reduce(requested_issue_ids, state, fn issue_id, state_acc ->
      if MapSet.member?(visible_issue_ids, issue_id) do
        state_acc
      else
        Logger.info("Blocked issue no longer visible during state refresh: issue_id=#{issue_id}; releasing block")
        release_issue_claim(state_acc, issue_id)
      end
    end)
  end

  defp reconcile_missing_blocked_issue_ids(state, _requested_issue_ids, _issues), do: state

  defp log_missing_running_issue(%State{} = state, issue_id) when is_binary(issue_id) do
    case Map.get(state.running, issue_id) do
      %{identifier: identifier} ->
        Logger.info("Issue no longer visible during running-state refresh: issue_id=#{issue_id} issue_identifier=#{identifier}; stopping active agent")

      _ ->
        Logger.info("Issue no longer visible during running-state refresh: issue_id=#{issue_id}; stopping active agent")
    end
  end

  defp log_missing_running_issue(_state, _issue_id), do: :ok

  defp refresh_running_issue_state(%State{} = state, %Issue{} = issue) do
    case Map.get(state.running, issue.id) do
      %{issue: _} = running_entry ->
        %{state | running: Map.put(state.running, issue.id, %{running_entry | issue: issue})}

      _ ->
        state
    end
  end

  defp refresh_blocked_issue_state(%State{} = state, %Issue{} = issue) do
    case Map.get(state.blocked, issue.id) do
      %{issue: _} = blocked_entry ->
        %{state | blocked: Map.put(state.blocked, issue.id, %{blocked_entry | issue: issue})}

      _ ->
        state
    end
  end

  defp terminate_running_issue(%State{} = state, issue_id, cleanup_workspace) do
    case Map.get(state.running, issue_id) do
      nil ->
        release_issue_claim(state, issue_id)

      %{pid: pid, ref: ref, identifier: identifier} = running_entry ->
        state = record_session_completion_totals(state, running_entry)

        stop_running_task(pid, ref, state.task_supervisor)

        if cleanup_workspace do
          cleanup_issue_workspace(Map.get(running_entry, :issue, identifier), running_entry)
        end

        %{
          state
          | running: Map.delete(state.running, issue_id),
            claimed: MapSet.delete(state.claimed, issue_id),
            blocked: Map.delete(state.blocked, issue_id),
            retry_attempts: Map.delete(state.retry_attempts, issue_id)
        }

      _ ->
        release_issue_claim(state, issue_id)
    end
  end

  defp reconcile_stalled_running_issues(%State{} = state) do
    timeout_ms = Config.settings!().codex.stall_timeout_ms

    cond do
      timeout_ms <= 0 ->
        state

      map_size(state.running) == 0 ->
        state

      true ->
        now = DateTime.utc_now()

        Enum.reduce(state.running, state, fn {issue_id, running_entry}, state_acc ->
          maybe_restart_stalled_issue(state_acc, issue_id, running_entry, now, timeout_ms)
        end)
    end
  end

  defp maybe_restart_stalled_issue(state, issue_id, running_entry, now, timeout_ms) do
    if Map.has_key?(state.blocked, issue_id) do
      state
    else
      restart_stalled_issue(state, issue_id, running_entry, now, timeout_ms)
    end
  end

  defp restart_stalled_issue(state, issue_id, running_entry, now, timeout_ms) do
    elapsed_ms = stall_elapsed_ms(running_entry, now)

    if is_integer(elapsed_ms) and elapsed_ms > timeout_ms do
      identifier = Map.get(running_entry, :identifier, issue_id)
      session_id = running_entry_session_id(running_entry)

      if input_required_blocker?(running_entry) do
        error = blocker_error(running_entry, "stalled for #{elapsed_ms}ms after Codex requested operator input")

        Logger.warning("Issue blocked: issue_id=#{issue_id} issue_identifier=#{identifier} session_id=#{session_id} elapsed_ms=#{elapsed_ms}; #{error}")

        state
        |> record_session_completion_totals(running_entry)
        |> stop_and_block_issue(issue_id, running_entry, error)
      else
        Logger.warning("Issue stalled: issue_id=#{issue_id} issue_identifier=#{identifier} session_id=#{session_id} elapsed_ms=#{elapsed_ms}; restarting with backoff")

        next_attempt = next_retry_attempt_from_running(running_entry)

        state
        |> terminate_running_issue(issue_id, false)
        |> schedule_issue_retry(issue_id, next_attempt, %{
          identifier: identifier,
          issue_url: running_entry.issue.url,
          error: "stalled for #{elapsed_ms}ms without codex activity"
        })
      end
    else
      state
    end
  end

  defp stall_elapsed_ms(running_entry, now) do
    running_entry
    |> last_activity_timestamp()
    |> case do
      %DateTime{} = timestamp ->
        max(0, DateTime.diff(now, timestamp, :millisecond))

      _ ->
        nil
    end
  end

  defp last_activity_timestamp(running_entry) when is_map(running_entry) do
    case Map.fetch(running_entry, :last_progress_timestamp) do
      {:ok, %DateTime{} = timestamp} ->
        timestamp

      {:ok, _missing_progress} ->
        Map.get(running_entry, :started_at)

      :error ->
        Map.get(running_entry, :last_codex_timestamp) || Map.get(running_entry, :started_at)
    end
  end

  defp last_activity_timestamp(_running_entry), do: nil

  defp input_required_blocker?(running_entry) when is_map(running_entry) do
    Map.get(running_entry, :last_codex_event) in [:turn_input_required, :approval_required] or
      not is_nil(input_required_completion_outcome(Map.get(running_entry, :completion))) or
      codex_message_method(Map.get(running_entry, :last_codex_message)) ==
        "mcpServer/elicitation/request"
  end

  defp input_required_blocker?(_running_entry), do: false

  defp input_required_completion_outcome(completion) when is_map(completion) do
    outcome = Map.get(completion, :outcome) || Map.get(completion, "outcome")
    normalize_input_required_outcome(outcome)
  end

  defp input_required_completion_outcome(_completion), do: nil

  defp normalize_input_required_outcome(outcome)
       when outcome in [:input_required, :needs_input, :approval_required],
       do: outcome

  defp normalize_input_required_outcome(outcome) when is_binary(outcome) do
    case outcome do
      "input_required" -> :input_required
      "needs_input" -> :needs_input
      "approval_required" -> :approval_required
      _ -> nil
    end
  end

  defp normalize_input_required_outcome(_outcome), do: nil

  defp blocker_error(running_entry, fallback) when is_map(running_entry) do
    codex_event_blocker_error(Map.get(running_entry, :last_codex_event)) ||
      completion_blocker_error(Map.get(running_entry, :completion)) ||
      codex_message_blocker_error(Map.get(running_entry, :last_codex_message)) ||
      fallback
  end

  defp blocker_error(_running_entry, fallback), do: fallback

  defp codex_event_blocker_error(:turn_input_required), do: "codex turn requires operator input"
  defp codex_event_blocker_error(:approval_required), do: "codex turn requires approval"
  defp codex_event_blocker_error(_event), do: nil

  defp completion_blocker_error(completion) do
    case input_required_completion_outcome(completion) do
      outcome when outcome in [:input_required, :needs_input] -> "codex turn requires operator input"
      :approval_required -> "codex turn requires approval"
      nil -> nil
    end
  end

  defp codex_message_blocker_error(message) do
    if codex_message_method(message) == "mcpServer/elicitation/request" do
      "codex MCP elicitation requires operator input"
    end
  end

  defp codex_message_method(%{message: %{"method" => method}}) when is_binary(method), do: method
  defp codex_message_method(%{message: %{method: method}}) when is_binary(method), do: method
  defp codex_message_method(%{"method" => method}) when is_binary(method), do: method
  defp codex_message_method(%{method: method}) when is_binary(method), do: method
  defp codex_message_method(_message), do: nil

  defp terminate_task(pid, task_supervisor) when is_pid(pid) do
    case Task.Supervisor.terminate_child(task_supervisor, pid) do
      :ok ->
        :ok

      {:error, :not_found} ->
        Process.exit(pid, :shutdown)
    end
  end

  defp terminate_task(_pid, _task_supervisor), do: :ok

  defp stop_running_task(pid, ref, task_supervisor) do
    if is_pid(pid) do
      terminate_task(pid, task_supervisor)
    end

    if is_reference(ref) do
      Process.demonitor(ref, [:flush])
    end

    :ok
  end

  defp stop_and_block_issue(%State{} = state, issue_id, running_entry, error) do
    stop_running_task(
      Map.get(running_entry, :pid),
      Map.get(running_entry, :ref),
      state.task_supervisor
    )

    block_issue_from_entry(state, issue_id, running_entry, error)
  end

  defp block_issue_from_entry(%State{} = state, issue_id, running_entry, error) do
    blocked_entry = %{
      issue_id: issue_id,
      identifier: Map.get(running_entry, :identifier, issue_id),
      issue: Map.get(running_entry, :issue),
      worker_host: Map.get(running_entry, :worker_host),
      workspace_path: Map.get(running_entry, :workspace_path),
      session_id: running_entry_session_id(running_entry),
      error: error,
      blocked_at: DateTime.utc_now(),
      last_codex_message: Map.get(running_entry, :last_codex_message),
      last_codex_event: Map.get(running_entry, :last_codex_event),
      last_codex_timestamp: Map.get(running_entry, :last_codex_timestamp),
      model_route: Map.get(running_entry, :model_route),
      selected_model_tier: Map.get(running_entry, :selected_model_tier),
      actual_model: Map.get(running_entry, :actual_model),
      routing_reason: Map.get(running_entry, :routing_reason),
      escalation_history: Map.get(running_entry, :escalation_history, [])
    }

    %{
      state
      | running: Map.delete(state.running, issue_id),
        retry_attempts: Map.delete(state.retry_attempts, issue_id),
        claimed: MapSet.put(state.claimed, issue_id),
        blocked: Map.put(state.blocked, issue_id, blocked_entry)
    }
  end

  defp choose_issues(issues, state) do
    active_states = active_state_set()
    terminal_states = terminal_state_set()
    owner_control = owner_control_dispatch_context()
    state = apply_owner_worker_limit(state, owner_control)

    issues
    |> sort_issues_for_dispatch(owner_control.resumable_issue_numbers)
    |> Enum.reduce(state, fn issue, state_acc ->
      maybe_dispatch_issue(
        issue,
        state_acc,
        active_states,
        terminal_states,
        owner_control
      )
    end)
  end

  defp maybe_dispatch_issue(issue, state, active_states, terminal_states, owner_control) do
    with true <-
           should_dispatch_issue?(
             issue,
             state,
             active_states,
             terminal_states,
             owner_control.intake_active,
             owner_control.ready_issue_numbers,
             owner_control.resumable_issue_numbers
           ),
         :ok <-
           acquire_owner_control_lease(
             issue,
             owner_control.ready_issue_numbers,
             owner_control.resumable_issue_numbers
           ) do
      dispatch_issue(state, issue)
    else
      _not_dispatchable -> state
    end
  end

  defp sort_issues_for_dispatch(issues, resumable_issue_numbers \\ %{}) when is_list(issues) do
    Enum.sort_by(issues, fn
      %Issue{} = issue ->
        {
          resumable_issue_rank(issue, resumable_issue_numbers),
          priority_rank(issue.priority),
          issue_created_at_sort_key(issue),
          issue.identifier || issue.id || ""
        }

      _ ->
        {1, priority_rank(nil), issue_created_at_sort_key(nil), ""}
    end)
  end

  defp resumable_issue_rank(issue, resumable_issue_numbers) do
    if resumable_issue?(issue, resumable_issue_numbers), do: 0, else: 1
  end

  defp priority_rank(priority) when is_integer(priority) and priority in 1..4, do: priority
  defp priority_rank(_priority), do: 5

  defp issue_created_at_sort_key(%Issue{created_at: %DateTime{} = created_at}) do
    DateTime.to_unix(created_at, :microsecond)
  end

  defp issue_created_at_sort_key(%Issue{}), do: 9_223_372_036_854_775_807
  defp issue_created_at_sort_key(_issue), do: 9_223_372_036_854_775_807

  defp should_dispatch_issue?(
         %Issue{} = issue,
         %State{running: running, claimed: claimed, blocked: blocked} = state,
         active_states,
         terminal_states,
         intake_active,
         ready_issue_numbers,
         resumable_issue_numbers
       ) do
    intake_active and
      candidate_issue?(
        issue,
        active_states,
        terminal_states,
        ready_issue_numbers,
        resumable_issue_numbers
      ) and
      !MapSet.member?(claimed, issue.id) and
      !Map.has_key?(running, issue.id) and
      !Map.has_key?(blocked, issue.id) and
      fresh_dispatch_slots_available?(state) and
      state_slots_available?(issue, running) and
      worker_slots_available?(state)
  end

  defp should_dispatch_issue?(
         _issue,
         _state,
         _active_states,
         _terminal_states,
         _intake_active,
         _ready_issue_numbers,
         _resumable_issue_numbers
       ),
       do: false

  defp state_slots_available?(%Issue{state: issue_state}, running) when is_map(running) do
    limit = Config.max_concurrent_agents_for_state(issue_state)
    used = running_issue_count_for_state(running, issue_state)
    limit > used
  end

  defp state_slots_available?(_issue, _running), do: false

  defp running_issue_count_for_state(running, issue_state) when is_map(running) do
    normalized_state = normalize_issue_state(issue_state)

    Enum.count(running, fn
      {_id, %{issue: %Issue{state: state_name}}} ->
        normalize_issue_state(state_name) == normalized_state

      _ ->
        false
    end)
  end

  defp candidate_issue?(
         %Issue{
           id: id,
           identifier: identifier,
           title: title,
           state: state_name
         } = issue,
         active_states,
         terminal_states,
         ready_issue_numbers,
         resumable_issue_numbers
       )
       when is_binary(id) and is_binary(identifier) and is_binary(title) and is_binary(state_name) do
    Enum.all?([id, identifier, title, state_name], &present_string?/1) and
      issue_dispatch_routable?(issue, ready_issue_numbers, resumable_issue_numbers) and
      active_issue_state?(state_name, active_states) and
      !terminal_issue_state?(state_name, terminal_states)
  end

  defp candidate_issue?(
         _issue,
         _active_states,
         _terminal_states,
         _ready_issue_numbers,
         _resumable_issue_numbers
       ),
       do: false

  defp issue_dispatch_routable?(%Issue{} = issue, ready_issue_numbers, resumable_issue_numbers)
       when is_map(ready_issue_numbers) and is_map(resumable_issue_numbers) do
    (Map.has_key?(ready_issue_numbers, issue.id) and
       (issue_routable?(issue) or issue_routable_without_owner_lease?(issue))) or
      resumable_issue?(issue, resumable_issue_numbers)
  end

  defp issue_dispatch_routable?(
         %Issue{} = issue,
         :owner_control_disabled,
         :owner_control_disabled
       ),
       do: issue_routable?(issue)

  defp resumable_issue?(%Issue{} = issue, resumable_issue_numbers)
       when is_map(resumable_issue_numbers) do
    Map.has_key?(resumable_issue_numbers, issue.id) and issue_routable?(issue)
  end

  defp resumable_issue?(_issue, _resumable_issue_numbers), do: false

  defp issue_routable_without_owner_lease?(%Issue{} = issue) do
    required_labels = Config.settings!().tracker.required_labels

    Enum.any?(required_labels, &(normalize_label(&1) == "symphony")) and
      Issue.routable?(issue, Enum.reject(required_labels, &(normalize_label(&1) == "symphony")))
  end

  defp issue_routable?(%Issue{} = issue) do
    Issue.routable?(issue, Config.settings!().tracker.required_labels)
  end

  defp terminal_issue_state?(state_name, terminal_states) when is_binary(state_name) do
    MapSet.member?(terminal_states, normalize_issue_state(state_name))
  end

  defp terminal_issue_state?(_state_name, _terminal_states), do: false

  defp present_string?(value) when is_binary(value), do: String.trim(value) != ""
  defp present_string?(_value), do: false

  defp active_issue_state?(state_name, active_states) when is_binary(state_name) do
    MapSet.member?(active_states, normalize_issue_state(state_name))
  end

  defp normalize_issue_state(state_name) when is_binary(state_name) do
    String.downcase(String.trim(state_name))
  end

  defp normalize_label(label) when is_binary(label) do
    label
    |> String.trim()
    |> String.downcase()
  end

  defp normalize_label(_label), do: ""

  defp terminal_state_set do
    Config.settings!().tracker.terminal_states
    |> Enum.map(&normalize_issue_state/1)
    |> Enum.filter(&(&1 != ""))
    |> MapSet.new()
  end

  defp active_state_set do
    Config.settings!().tracker.active_states
    |> Enum.map(&normalize_issue_state/1)
    |> Enum.filter(&(&1 != ""))
    |> MapSet.new()
  end

  defp dispatch_issue(
         %State{} = state,
         issue,
         attempt \\ nil,
         preferred_worker_host \\ nil,
         model_route \\ nil
       ) do
    case refresh_issue_for_dispatch(issue) do
      {:ok, %Issue{} = refreshed_issue} ->
        do_dispatch_issue(state, refreshed_issue, attempt, preferred_worker_host, model_route)

      {:skip, _reason} ->
        state

      {:error, _reason} ->
        state
    end
  end

  defp refresh_issue_for_dispatch(issue) do
    case revalidate_issue_for_dispatch(issue, &Tracker.fetch_issues_by_ids/1, terminal_state_set()) do
      {:ok, %Issue{} = refreshed_issue} ->
        {:ok, refreshed_issue}

      {:skip, :missing} ->
        Logger.info("Skipping dispatch; issue no longer active or visible: #{issue_context(issue)}")
        {:skip, :missing}

      {:skip, %Issue{} = refreshed_issue} ->
        Logger.info("Skipping stale dispatch after issue refresh: #{issue_context(refreshed_issue)} state=#{inspect(refreshed_issue.state)} blocked_by=#{length(refreshed_issue.blocked_by)}")

        {:skip, refreshed_issue}

      {:error, reason} ->
        Logger.warning("Skipping dispatch; issue refresh failed for #{issue_context(issue)}: #{inspect(reason)}")
        {:error, reason}
    end
  end

  defp do_dispatch_issue(%State{} = state, issue, attempt, preferred_worker_host, model_route) do
    recipient = self()

    case select_worker_host(state, preferred_worker_host) do
      :no_worker_capacity ->
        Logger.debug("No SSH worker slots available for #{issue_context(issue)} preferred_worker_host=#{inspect(preferred_worker_host)}")
        state

      worker_host ->
        spawn_issue_on_worker_host(state, issue, attempt, recipient, worker_host, model_route)
    end
  end

  defp spawn_issue_on_worker_host(
         %State{} = state,
         issue,
         attempt,
         recipient,
         worker_host,
         model_route
       ) do
    task = fn -> run_agent_task(issue, recipient, attempt, worker_host, model_route) end

    case Task.Supervisor.start_child(state.task_supervisor, task) do
      {:ok, pid} ->
        ref = Process.monitor(pid)

        Logger.info("Dispatching issue to agent: #{issue_context(issue)} pid=#{inspect(pid)} attempt=#{inspect(attempt)} worker_host=#{worker_host || "local"}")

        running_entry = new_running_entry(issue, pid, ref, worker_host, model_route, attempt)
        running = Map.put(state.running, issue.id, running_entry)

        %{
          state
          | running: running,
            claimed: MapSet.put(state.claimed, issue.id),
            retry_attempts: Map.delete(state.retry_attempts, issue.id)
        }

      {:error, reason} ->
        Logger.error("Unable to spawn agent for #{issue_context(issue)}: #{inspect(reason)}")
        next_attempt = if is_integer(attempt), do: attempt + 1, else: nil

        schedule_issue_retry(state, issue.id, next_attempt, %{
          identifier: issue.identifier,
          issue_url: issue.url,
          error: "failed to spawn agent: #{inspect(reason)}",
          worker_host: worker_host
        })
    end
  end

  defp run_agent_task(issue, recipient, attempt, worker_host, model_route) do
    case AgentRunner.run(issue, recipient,
           attempt: attempt,
           worker_host: worker_host,
           model_route: model_route
         ) do
      :ok -> :ok
      {:model_exhausted, route, reason} -> exit({:model_exhausted, route, reason})
      {:workspace_hook_failed, "before_run", _status, _output} = reason -> exit(reason)
    end
  end

  defp new_running_entry(issue, pid, ref, worker_host, model_route, attempt) do
    route = model_route || %{}
    started_at = DateTime.utc_now()

    %{
      pid: pid,
      ref: ref,
      identifier: issue.identifier,
      issue: issue,
      worker_host: worker_host,
      workspace_path: nil,
      session_id: nil,
      last_codex_message: nil,
      last_codex_timestamp: nil,
      last_progress_timestamp: started_at,
      last_codex_event: nil,
      codex_app_server_pid: nil,
      codex_input_tokens: 0,
      codex_output_tokens: 0,
      codex_total_tokens: 0,
      codex_last_reported_input_tokens: 0,
      codex_last_reported_output_tokens: 0,
      codex_last_reported_total_tokens: 0,
      turn_count: 0,
      model_route: model_route,
      selected_model_tier: Map.get(route, :selected_tier),
      actual_model: Map.get(route, :actual_model),
      routing_reason: Map.get(route, :routing_reason),
      escalated_from: Map.get(route, :escalated_from),
      escalation_history: Map.get(route, :escalation_history, []),
      retry_attempt: normalize_retry_attempt(attempt),
      started_at: started_at
    }
  end

  defp revalidate_issue_for_dispatch(%Issue{id: issue_id}, issue_fetcher, terminal_states)
       when is_binary(issue_id) and is_function(issue_fetcher, 1) do
    case issue_fetcher.([issue_id]) do
      {:ok, [%Issue{} = refreshed_issue | _]} ->
        if retry_candidate_issue?(refreshed_issue, terminal_states) do
          {:ok, refreshed_issue}
        else
          {:skip, refreshed_issue}
        end

      {:ok, []} ->
        {:skip, :missing}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp revalidate_issue_for_dispatch(issue, _issue_fetcher, _terminal_states), do: {:ok, issue}

  defp complete_issue(%State{} = state, issue_id) do
    %{
      state
      | completed: MapSet.put(state.completed, issue_id),
        retry_attempts: Map.delete(state.retry_attempts, issue_id)
    }
  end

  defp schedule_issue_retry(%State{} = state, issue_id, attempt, metadata)
       when is_binary(issue_id) and is_map(metadata) do
    previous_retry = Map.get(state.retry_attempts, issue_id, %{attempt: 0})
    next_attempt = retry_attempt_number(attempt, previous_retry)
    delay_type = Map.get(metadata, :delay_type, Map.get(previous_retry, :delay_type, :failure))
    delay_ms = retry_delay(next_attempt, delay_type)
    old_timer = Map.get(previous_retry, :timer_ref)
    retry_token = make_ref()
    due_at_ms = System.monotonic_time(:millisecond) + delay_ms
    identifier = pick_retry_identifier(issue_id, previous_retry, metadata)
    issue_url = pick_retry_issue_url(previous_retry, metadata)
    error = pick_retry_error(previous_retry, metadata)
    worker_host = pick_retry_worker_host(previous_retry, metadata)
    workspace_path = pick_retry_workspace_path(previous_retry, metadata)
    model_route = metadata[:model_route] || Map.get(previous_retry, :model_route)
    escalation_reason = metadata[:escalation_reason] || Map.get(previous_retry, :escalation_reason)

    deferred_reason = retry_deferred_reason(delay_type, metadata, previous_retry)
    cancel_retry_timer(old_timer)

    timer_ref = Process.send_after(self(), {:retry_issue, issue_id, retry_token}, delay_ms)
    log_retry_schedule(issue_id, identifier, delay_ms, next_attempt, error, delay_type, deferred_reason)

    %{
      state
      | retry_attempts:
          Map.put(state.retry_attempts, issue_id, %{
            attempt: next_attempt,
            timer_ref: timer_ref,
            retry_token: retry_token,
            due_at_ms: due_at_ms,
            identifier: identifier,
            issue_url: issue_url,
            error: error,
            worker_host: worker_host,
            workspace_path: workspace_path,
            model_route: model_route,
            escalation_reason: escalation_reason,
            delay_type: delay_type,
            deferred_reason: deferred_reason
          })
    }
  end

  defp retry_attempt_number(attempt, _previous_retry) when is_integer(attempt), do: attempt
  defp retry_attempt_number(_attempt, previous_retry), do: previous_retry.attempt + 1

  defp retry_deferred_reason(delay_type, metadata, previous_retry)
       when delay_type in [:capacity, :paused, :source_circuit] do
    Map.get(metadata, :deferred_reason) || Map.get(previous_retry, :deferred_reason)
  end

  defp retry_deferred_reason(_delay_type, _metadata, _previous_retry), do: nil

  defp cancel_retry_timer(timer_ref) when is_reference(timer_ref) do
    Process.cancel_timer(timer_ref)
    :ok
  end

  defp cancel_retry_timer(_timer_ref), do: :ok

  defp log_retry_schedule(
         issue_id,
         identifier,
         delay_ms,
         attempt,
         error,
         delay_type,
         deferred_reason
       ) do
    error_suffix = if is_binary(error), do: " error=#{error}", else: ""

    message =
      "Retrying issue_id=#{issue_id} issue_identifier=#{identifier} in #{delay_ms}ms " <>
        "(attempt #{attempt})#{error_suffix}"

    log_retry_message(message, delay_type, deferred_reason)
  end

  defp log_retry_message(message, delay_type, deferred_reason)
       when delay_type in [:capacity, :paused, :source_circuit] do
    Logger.debug("#{message} deferred=#{deferred_reason}")
  end

  defp log_retry_message(message, _delay_type, _deferred_reason), do: Logger.warning(message)

  defp pop_retry_attempt_state(%State{} = state, issue_id, retry_token) when is_reference(retry_token) do
    case Map.get(state.retry_attempts, issue_id) do
      %{attempt: attempt, retry_token: ^retry_token} = retry_entry ->
        metadata = %{
          identifier: Map.get(retry_entry, :identifier),
          issue_url: Map.get(retry_entry, :issue_url),
          error: Map.get(retry_entry, :error),
          worker_host: Map.get(retry_entry, :worker_host),
          workspace_path: Map.get(retry_entry, :workspace_path),
          model_route: Map.get(retry_entry, :model_route),
          escalation_reason: Map.get(retry_entry, :escalation_reason),
          delay_type: Map.get(retry_entry, :delay_type, :failure),
          deferred_reason: Map.get(retry_entry, :deferred_reason)
        }

        {:ok, attempt, metadata, %{state | retry_attempts: Map.delete(state.retry_attempts, issue_id)}}

      _ ->
        :missing
    end
  end

  defp handle_retry_issue(%State{} = state, issue_id, attempt, metadata) do
    if source_circuit_open?(state) do
      {:noreply, defer_retry_for_source_circuit(state, issue_id, attempt, metadata)}
    else
      case Tracker.fetch_issues_by_ids([issue_id]) do
        {:ok, issues} ->
          state = source_circuit_success(state)

          issues
          |> find_issue_by_id(issue_id)
          |> handle_retry_issue_lookup(state, issue_id, attempt, metadata)

        {:error, reason} ->
          Logger.warning("Retry poll failed for issue_id=#{issue_id} issue_identifier=#{metadata[:identifier] || issue_id}: #{inspect(reason)}")
          state = source_circuit_failure(state, reason)

          {:noreply,
           schedule_issue_retry(
             state,
             issue_id,
             attempt,
             Map.merge(metadata, %{
               error: "shared source unavailable: #{inspect(reason)}",
               delay_type: :source_circuit,
               deferred_reason: "shared issue source unavailable"
             })
           )}
      end
    end
  end

  defp defer_retry_for_source_circuit(state, issue_id, attempt, metadata) do
    schedule_issue_retry(
      state,
      issue_id,
      attempt,
      Map.merge(metadata, %{
        delay_type: :source_circuit,
        deferred_reason: "shared issue source circuit is open"
      })
    )
  end

  defp handle_retry_issue_lookup(%Issue{} = issue, state, issue_id, attempt, metadata) do
    terminal_states = terminal_state_set()

    cond do
      terminal_issue_state?(issue.state, terminal_states) ->
        Logger.info("Issue state is terminal: issue_id=#{issue_id} issue_identifier=#{issue.identifier} state=#{issue.state}; removing associated workspace")

        cleanup_issue_workspace(issue, metadata)
        {:noreply, release_issue_claim(state, issue_id)}

      normal_completion_postcondition_required?(issue, metadata) ->
        handle_normal_completion_postcondition(state, issue, attempt, metadata)

      retry_candidate_issue?(issue, terminal_states) ->
        handle_active_retry(state, issue, attempt, metadata)

      true ->
        Logger.debug("Issue left active states, removing claim issue_id=#{issue_id} issue_identifier=#{issue.identifier}")

        {:noreply, release_issue_claim(state, issue_id)}
    end
  end

  defp handle_retry_issue_lookup(nil, state, issue_id, _attempt, _metadata) do
    Logger.debug("Issue no longer visible, removing claim issue_id=#{issue_id}")
    {:noreply, release_issue_claim(state, issue_id)}
  end

  defp normal_completion_postcondition_required?(%Issue{} = issue, metadata) when is_map(metadata) do
    Map.get(metadata, :delay_type) in [:continuation, :completion_postcondition] and
      owner_control_enabled?() and !symphony_lease_present?(issue)
  end

  defp symphony_lease_present?(%Issue{labels: labels}) when is_list(labels) do
    Enum.any?(labels, &(normalize_label(&1) == "symphony"))
  end

  defp symphony_lease_present?(_issue), do: false

  defp handle_normal_completion_postcondition(state, issue, attempt, metadata) do
    with {:ok, issue_number} <- owner_control_issue_number(issue),
         client <- Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient),
         {:ok, _response} <- request_owner_control_complete_run(client, issue_number) do
      Logger.info("Completed normal Symphony run in Owner Control: #{issue_context(issue)}")
      {:noreply, release_issue_claim(state, issue.id)}
    else
      error -> retry_normal_completion_postcondition(state, issue, attempt, metadata, error)
    end
  end

  defp owner_control_issue_number(%Issue{id: issue_id}) do
    case Integer.parse(to_string(issue_id || "")) do
      {issue_number, ""} when issue_number > 0 -> {:ok, issue_number}
      _ -> {:error, :owner_control_issue_number_unavailable}
    end
  end

  defp request_owner_control_complete_run(client, issue_number)
       when is_atom(client) and is_integer(issue_number) and issue_number > 0 do
    if function_exported?(client, :complete_run, 1) do
      client.complete_run(issue_number)
    else
      {:error, :owner_control_complete_run_unavailable}
    end
  end

  defp request_owner_control_complete_run(_client, _issue_number),
    do: {:error, :owner_control_complete_run_unavailable}

  defp retry_normal_completion_postcondition(state, issue, attempt, metadata, reason) do
    Logger.warning("Normal completion postcondition unresolved for #{issue_context(issue)}: #{inspect(reason)}")

    {:noreply,
     schedule_issue_retry(
       state,
       issue.id,
       attempt + 1,
       Map.merge(metadata, %{
         identifier: issue.identifier,
         issue_url: issue.url,
         error: "normal completion postcondition unresolved: #{inspect(reason)}",
         delay_type: :completion_postcondition,
         deferred_reason: nil
       })
     )}
  end

  defp cleanup_issue_workspace(identifier, worker_host \\ nil)

  defp cleanup_issue_workspace(issue_or_identifier, metadata) when is_map(metadata) do
    case Map.get(metadata, :workspace_path) do
      workspace_path when is_binary(workspace_path) and workspace_path != "" ->
        Workspace.remove_recorded(workspace_path, Map.get(metadata, :worker_host))

      _ ->
        cleanup_issue_workspace(issue_or_identifier, Map.get(metadata, :worker_host))
    end
  end

  defp cleanup_issue_workspace(%Issue{} = issue, worker_host) do
    Workspace.remove_issue_workspaces(issue, worker_host)
  end

  defp cleanup_issue_workspace(identifier, worker_host) when is_binary(identifier) do
    Workspace.remove_issue_workspaces(identifier, worker_host)
  end

  defp cleanup_issue_workspace(_issue_or_identifier, _worker_host), do: :ok

  defp start_terminal_workspace_cleanup(
         %State{task_supervisor: task_supervisor},
         cleanup_config
       ) do
    if terminal_workspace_cleanup_needed?(cleanup_config) do
      do_start_terminal_workspace_cleanup(task_supervisor, self(), cleanup_config)
    end
  end

  defp do_start_terminal_workspace_cleanup(task_supervisor, orchestrator, cleanup_config) do
    case Task.Supervisor.start_child(task_supervisor, fn ->
           run_terminal_workspace_cleanup_with_config(orchestrator, cleanup_config)
         end) do
      {:ok, _pid} ->
        :ok

      {:error, reason} ->
        Logger.warning("Skipping startup terminal workspace cleanup; task unavailable: #{inspect(reason)}")
    end
  end

  defp run_terminal_workspace_cleanup_with_config(orchestrator, cleanup_config) do
    Config.with_settings_snapshot(cleanup_config, fn ->
      run_terminal_workspace_cleanup(orchestrator, cleanup_config.tracker.terminal_states)
    end)
  end

  defp startup_cleanup_config(%{worker: %{ssh_hosts: []}} = config, workflow_path) do
    workspace_root =
      config.workspace.root
      |> Path.expand(workflow_path |> Path.expand() |> Path.dirname())

    put_in(config.workspace.root, workspace_root)
  end

  defp startup_cleanup_config(config, _workflow_path), do: config

  defp terminal_workspace_cleanup_needed?(%{worker: %{ssh_hosts: []}, workspace: %{root: root}}) do
    case File.ls(root) do
      {:ok, entries} -> entries != []
      {:error, :enoent} -> false
      {:error, _reason} -> true
    end
  end

  defp terminal_workspace_cleanup_needed?(_config), do: true

  defp run_terminal_workspace_cleanup(orchestrator, terminal_state_names) do
    case Tracker.fetch_issues_by_states(terminal_state_names) do
      {:ok, issues} ->
        Enum.each(issues, &cleanup_terminal_issue_candidate(orchestrator, &1))

      {:error, reason} ->
        Logger.warning("Skipping startup terminal workspace cleanup; failed to fetch terminal issues: #{inspect(reason)}")
    end
  end

  defp cleanup_terminal_issue_candidate(orchestrator, %Issue{} = issue) do
    if terminal_issue_workspace_may_exist?(issue) do
      cleanup_terminal_issue_workspace(orchestrator, issue)
    end
  end

  defp cleanup_terminal_issue_candidate(_orchestrator, _issue), do: :ok

  defp terminal_issue_workspace_may_exist?(%Issue{} = issue) do
    case Config.settings!().worker.ssh_hosts do
      [] ->
        Config.local_workspace_root()
        |> Path.join(Workspace.workspace_key(issue))
        |> File.exists?()

      _worker_hosts ->
        true
    end
  end

  defp cleanup_terminal_issue_workspace(orchestrator, %Issue{id: issue_id} = issue)
       when is_binary(issue_id) do
    case GenServer.call(orchestrator, {:reserve_startup_workspace_cleanup, issue_id}, :infinity) do
      :reserved ->
        try do
          cleanup_issue_workspace(issue)
        after
          GenServer.call(orchestrator, {:release_startup_workspace_cleanup, issue_id}, :infinity)
        end

      :busy ->
        :ok
    end
  end

  defp cleanup_terminal_issue_workspace(_orchestrator, _issue), do: :ok

  defp notify_dashboard do
    StatusDashboard.notify_update()
  end

  defp handle_active_retry(state, issue, attempt, metadata) do
    intake_active = owner_control_intake_active?()

    if retry_dispatch_allowed?(intake_active, issue, state, metadata[:worker_host]) do
      case refresh_issue_for_dispatch(issue) do
        {:ok, %Issue{} = refreshed_issue} ->
          {:noreply,
           do_dispatch_issue(
             state,
             refreshed_issue,
             attempt,
             metadata[:worker_host],
             metadata[:model_route]
           )}

        {:skip, :missing} ->
          {:noreply, release_issue_claim(state, issue.id)}

        {:skip, %Issue{} = refreshed_issue} ->
          handle_retry_issue_lookup(refreshed_issue, state, issue.id, attempt, metadata)

        {:error, reason} ->
          {:noreply,
           schedule_issue_retry(
             state,
             issue.id,
             attempt + 1,
             Map.merge(metadata, %{
               identifier: issue.identifier,
               error: "retry dispatch refresh failed: #{inspect(reason)}",
               delay_type: :failure,
               deferred_reason: nil
             })
           )}
      end
    else
      {delay_type, retry_reason} =
        if intake_active,
          do: {:capacity, "no available orchestrator slots"},
          else: {:paused, "intake paused"}

      Logger.debug("Retry deferred for #{issue_context(issue)} reason=#{retry_reason}")

      {:noreply,
       schedule_issue_retry(
         state,
         issue.id,
         attempt,
         Map.merge(metadata, %{
           identifier: issue.identifier,
           delay_type: delay_type,
           deferred_reason: retry_reason
         })
       )}
    end
  end

  defp retry_dispatch_allowed?(intake_active, issue, state, worker_host) do
    intake_active and
      retry_candidate_issue?(issue, terminal_state_set()) and
      dispatch_slots_available?(issue, state) and
      worker_slots_available?(state, worker_host)
  end

  defp owner_control_dispatch_context do
    if owner_control_enabled?() do
      client = Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient)

      case owner_control_intake_active?() do
        true -> owner_control_ready_context(client)
        false -> empty_owner_control_dispatch_context(false)
      end
    else
      legacy_owner_control_dispatch_context()
    end
  rescue
    _exception -> empty_owner_control_dispatch_context(false)
  catch
    _kind, _reason -> empty_owner_control_dispatch_context(false)
  end

  defp owner_control_ready_context(client) do
    case client.snapshot() do
      {:ok, snapshot} when is_map(snapshot) ->
        %{
          intake_active: true,
          ready_issue_numbers: fresh_ready_issue_numbers(snapshot),
          resumable_issue_numbers: fresh_resumable_issue_numbers(snapshot),
          worker_limit: fresh_owner_worker_limit(snapshot)
        }

      _disabled_or_unavailable ->
        empty_owner_control_dispatch_context(true)
    end
  end

  defp empty_owner_control_dispatch_context(intake_active) do
    %{
      intake_active: intake_active,
      ready_issue_numbers: %{},
      resumable_issue_numbers: %{},
      worker_limit: nil
    }
  end

  defp legacy_owner_control_dispatch_context do
    %{
      intake_active: true,
      ready_issue_numbers: :owner_control_disabled,
      resumable_issue_numbers: :owner_control_disabled,
      worker_limit: nil
    }
  end

  defp fresh_owner_worker_limit(snapshot) do
    limit = get_in(snapshot, [:workers, :limit])
    configured_max = Config.settings!().agent.max_concurrent_agents

    if fresh_owner_control_snapshot?(snapshot) and is_integer(limit) and limit in 1..configured_max,
      do: limit,
      else: nil
  end

  defp apply_owner_worker_limit(%State{} = state, %{worker_limit: limit})
       when is_integer(limit) and limit > 0,
       do: %{state | max_concurrent_agents: limit}

  defp apply_owner_worker_limit(%State{} = state, _owner_control), do: state

  defp owner_control_enabled? do
    not is_nil(Application.get_env(:symphony_elixir, :owner_control_client_module)) or
      OwnerControlClient.enabled?()
  end

  defp fresh_ready_issue_numbers(snapshot) do
    if fresh_owner_control_snapshot?(snapshot) do
      snapshot
      |> ready_issue_numbers()
      |> drop_quarantined_issue_numbers(snapshot)
    else
      %{}
    end
  end

  defp fresh_resumable_issue_numbers(snapshot) do
    if fresh_owner_control_snapshot?(snapshot) do
      snapshot
      |> project_issue_numbers("in progress")
      |> drop_quarantined_issue_numbers(snapshot)
    else
      %{}
    end
  end

  defp fresh_owner_control_snapshot?(snapshot) when is_map(snapshot) do
    Map.get(snapshot, :stale) == false and
      get_in(snapshot, [:sources, :github, :status]) == "fresh"
  end

  defp ready_issue_numbers(snapshot), do: project_issue_numbers(snapshot, "ready for ai")

  defp project_issue_numbers(%{issues: issues}, expected_status) when is_map(issues) do
    Enum.reduce(issues, %{}, fn
      {key, %{number: number, status: status, state: state}}, ready
      when is_binary(key) and is_integer(number) and number > 0 and is_binary(status) and
             is_binary(state) ->
        canonical_number = Integer.to_string(number)

        if key == canonical_number and normalize_issue_state(status) == expected_status and
             normalize_issue_state(state) == "open" do
          Map.put(ready, canonical_number, number)
        else
          ready
        end

      _entry, ready ->
        ready
    end)
  end

  defp project_issue_numbers(_snapshot, _expected_status), do: %{}

  defp quarantined_issue_keys(%{quarantined: quarantines}) do
    for %{issue: issue_number} <- List.wrap(quarantines),
        is_integer(issue_number) and issue_number > 0,
        do: Integer.to_string(issue_number)
  end

  defp quarantined_issue_keys(_snapshot), do: []

  defp drop_quarantined_issue_numbers(issue_numbers, snapshot) when is_map(issue_numbers),
    do: Map.drop(issue_numbers, quarantined_issue_keys(snapshot))

  defp acquire_owner_control_lease(
         %Issue{} = issue,
         ready_issue_numbers,
         resumable_issue_numbers
       )
       when is_map(ready_issue_numbers) and is_map(resumable_issue_numbers) do
    case owner_control_intake_active?() do
      true ->
        acquire_owner_control_lease_while_active(
          issue,
          ready_issue_numbers,
          resumable_issue_numbers
        )

      false ->
        Logger.info("Skipping dispatch while Owner Control intake is paused for #{issue_context(issue)}")
        {:error, :intake_paused}
    end
  rescue
    exception ->
      Logger.warning("Skipping dispatch; Owner Control lease request failed for #{issue_context(issue)}: #{Exception.message(exception)}")

      {:error, exception}
  catch
    kind, reason ->
      Logger.warning("Skipping dispatch; Owner Control lease request failed for #{issue_context(issue)}: #{inspect({kind, reason})}")

      {:error, {kind, reason}}
  end

  defp acquire_owner_control_lease(
         %Issue{},
         :owner_control_disabled,
         :owner_control_disabled
       ),
       do: :ok

  defp acquire_owner_control_lease_while_active(
         %Issue{} = issue,
         ready_issue_numbers,
         resumable_issue_numbers
       ) do
    if resumable_issue?(issue, resumable_issue_numbers) do
      :ok
    else
      request_owner_control_lease(issue, ready_issue_numbers)
    end
  end

  defp request_owner_control_lease(%Issue{} = issue, ready_issue_numbers) do
    with {:ok, issue_number} <- Map.fetch(ready_issue_numbers, issue.id),
         client <- Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient),
         {:ok, response} when is_map(response) <- client.action(:lease, %{issue: issue_number}) do
      :ok
    else
      error ->
        Logger.warning("Skipping dispatch; failed to acquire Owner Control lease for #{issue_context(issue)}: #{inspect(error)}")

        {:error, error}
    end
  end

  defp owner_control_intake_active? do
    client = Application.get_env(:symphony_elixir, :owner_control_client_module, OwnerControlClient)
    client.intake_active?()
  rescue
    _exception -> false
  catch
    _kind, _reason -> false
  end

  defp release_issue_claim(%State{} = state, issue_id) do
    %{
      state
      | claimed: MapSet.delete(state.claimed, issue_id),
        blocked: Map.delete(state.blocked, issue_id),
        retry_attempts: Map.delete(state.retry_attempts, issue_id)
    }
  end

  defp retry_delay(attempt, delay_type) when is_integer(attempt) and attempt > 0 do
    case delay_type do
      :continuation when attempt == 1 -> @continuation_retry_delay_ms
      :capacity -> @capacity_retry_delay_ms
      :paused -> @paused_retry_delay_ms
      :source_circuit -> @paused_retry_delay_ms
      _failure -> failure_retry_delay(attempt)
    end
  end

  defp wake_paused_retries(%State{} = state) do
    if owner_control_intake_active?() do
      now_ms = System.monotonic_time(:millisecond)
      retry_attempts = Map.new(state.retry_attempts, &wake_paused_retry_entry(&1, now_ms))
      %{state | retry_attempts: retry_attempts}
    else
      state
    end
  end

  defp wake_paused_retry_entry({issue_id, retry_entry}, now_ms) do
    if paused_retry?(retry_entry) do
      cancel_retry_timer(Map.get(retry_entry, :timer_ref))
      retry_token = make_ref()
      send(self(), {:retry_issue, issue_id, retry_token})

      {issue_id,
       retry_entry
       |> Map.put(:timer_ref, nil)
       |> Map.put(:retry_token, retry_token)
       |> Map.put(:due_at_ms, now_ms)
       |> Map.put(:delay_type, :capacity)
       |> Map.put(:deferred_reason, "intake resumed")}
    else
      {issue_id, retry_entry}
    end
  end

  defp paused_retry?(retry_entry) when is_map(retry_entry) do
    Map.get(retry_entry, :delay_type) == :paused or Map.get(retry_entry, :error) == "intake paused"
  end

  defp paused_retry?(_retry_entry), do: false

  defp failure_retry_delay(attempt) do
    max_delay_power = min(attempt - 1, 10)
    min(@failure_retry_base_ms * (1 <<< max_delay_power), Config.settings!().agent.max_retry_backoff_ms)
  end

  defp source_circuit_open?(%State{source_circuit: circuit}) do
    SourceCircuit.open?(circuit, System.monotonic_time(:millisecond))
  end

  defp source_circuit_success(%State{} = state) do
    %{state | source_circuit: SourceCircuit.success(state.source_circuit)}
  end

  defp source_circuit_failure(%State{} = state, reason) do
    circuit =
      SourceCircuit.failure(
        state.source_circuit,
        reason,
        System.monotonic_time(:millisecond)
      )

    %{state | source_circuit: circuit}
  end

  defp normalize_retry_attempt(attempt) when is_integer(attempt) and attempt > 0, do: attempt
  defp normalize_retry_attempt(_attempt), do: 0

  defp next_retry_attempt_from_running(running_entry) do
    case Map.get(running_entry, :retry_attempt) do
      attempt when is_integer(attempt) and attempt > 0 -> attempt + 1
      _ -> nil
    end
  end

  defp pick_retry_identifier(issue_id, previous_retry, metadata) do
    metadata[:identifier] || Map.get(previous_retry, :identifier) || issue_id
  end

  defp pick_retry_issue_url(previous_retry, metadata) do
    metadata[:issue_url] || Map.get(previous_retry, :issue_url)
  end

  defp pick_retry_error(previous_retry, metadata) do
    metadata[:error] || Map.get(previous_retry, :error)
  end

  defp pick_retry_worker_host(previous_retry, metadata) do
    metadata[:worker_host] || Map.get(previous_retry, :worker_host)
  end

  defp pick_retry_workspace_path(previous_retry, metadata) do
    metadata[:workspace_path] || Map.get(previous_retry, :workspace_path)
  end

  defp maybe_put_runtime_value(running_entry, _key, nil), do: running_entry

  defp maybe_put_runtime_value(running_entry, key, value) when is_map(running_entry) do
    Map.put(running_entry, key, value)
  end

  defp select_worker_host(%State{} = state, preferred_worker_host) do
    case Config.settings!().worker.ssh_hosts do
      [] ->
        nil

      hosts ->
        available_hosts = Enum.filter(hosts, &worker_host_slots_available?(state, &1))

        cond do
          available_hosts == [] ->
            :no_worker_capacity

          preferred_worker_host_available?(preferred_worker_host, available_hosts) ->
            preferred_worker_host

          true ->
            least_loaded_worker_host(state, available_hosts)
        end
    end
  end

  defp preferred_worker_host_available?(preferred_worker_host, hosts)
       when is_binary(preferred_worker_host) and is_list(hosts) do
    preferred_worker_host != "" and preferred_worker_host in hosts
  end

  defp preferred_worker_host_available?(_preferred_worker_host, _hosts), do: false

  defp least_loaded_worker_host(%State{} = state, hosts) when is_list(hosts) do
    hosts
    |> Enum.with_index()
    |> Enum.min_by(fn {host, index} ->
      {running_worker_host_count(state.running, host), index}
    end)
    |> elem(0)
  end

  defp running_worker_host_count(running, worker_host) when is_map(running) and is_binary(worker_host) do
    Enum.count(running, fn
      {_issue_id, %{worker_host: ^worker_host}} -> true
      _ -> false
    end)
  end

  defp worker_slots_available?(%State{} = state) do
    select_worker_host(state, nil) != :no_worker_capacity
  end

  defp worker_slots_available?(%State{} = state, preferred_worker_host) do
    select_worker_host(state, preferred_worker_host) != :no_worker_capacity
  end

  defp worker_host_slots_available?(%State{} = state, worker_host) when is_binary(worker_host) do
    case Config.settings!().worker.max_concurrent_agents_per_host do
      limit when is_integer(limit) and limit > 0 ->
        running_worker_host_count(state.running, worker_host) < limit

      _ ->
        true
    end
  end

  defp find_issue_by_id(issues, issue_id) when is_binary(issue_id) do
    Enum.find(issues, fn
      %Issue{id: ^issue_id} ->
        true

      _ ->
        false
    end)
  end

  defp find_issue_id_for_ref(running, ref) do
    running
    |> Enum.find_value(fn {issue_id, %{ref: running_ref}} ->
      if running_ref == ref, do: issue_id
    end)
  end

  defp running_entry_session_id(%{session_id: session_id}) when is_binary(session_id),
    do: session_id

  defp running_entry_session_id(_running_entry), do: "n/a"

  defp issue_context(%Issue{id: issue_id, identifier: identifier}) do
    "issue_id=#{issue_id} issue_identifier=#{identifier}"
  end

  defp available_slots(%State{} = state) do
    max(
      (state.max_concurrent_agents || Config.settings!().agent.max_concurrent_agents) -
        map_size(state.running),
      0
    )
  end

  defp fresh_dispatch_slots_available?(%State{} = state) do
    # A retry is already claimed owner work. Count it against the same WIP ceiling
    # so a fast poll cannot replace a continuation before its timer fires.
    available_slots(state) > map_size(state.retry_attempts)
  end

  @spec request_refresh() :: map() | :unavailable
  def request_refresh do
    request_refresh(__MODULE__)
  end

  @spec request_refresh(GenServer.server()) :: map() | :unavailable
  def request_refresh(server) do
    if Process.whereis(server) do
      GenServer.call(server, :request_refresh)
    else
      :unavailable
    end
  end

  @spec snapshot() :: map() | :timeout | :unavailable
  def snapshot, do: snapshot(__MODULE__, 15_000)

  @spec snapshot(GenServer.server(), timeout()) :: map() | :timeout | :unavailable
  def snapshot(server, timeout) do
    if Process.whereis(server) do
      try do
        GenServer.call(server, :snapshot, timeout)
      catch
        :exit, {:timeout, _} -> :timeout
        :exit, _ -> :unavailable
      end
    else
      :unavailable
    end
  end

  @impl true
  def handle_call({:reserve_startup_workspace_cleanup, issue_id}, _from, state) do
    if Map.has_key?(state.running, issue_id) or MapSet.member?(state.claimed, issue_id) do
      {:reply, :busy, state}
    else
      {:reply, :reserved, %{state | claimed: MapSet.put(state.claimed, issue_id)}}
    end
  end

  def handle_call({:release_startup_workspace_cleanup, issue_id}, _from, state) do
    {:reply, :ok, %{state | claimed: MapSet.delete(state.claimed, issue_id)}}
  end

  def handle_call(:snapshot, _from, state) do
    state = refresh_runtime_config(state)
    now = DateTime.utc_now()
    now_ms = System.monotonic_time(:millisecond)

    running =
      state.running
      |> Enum.map(fn {issue_id, metadata} ->
        %{
          issue_id: issue_id,
          identifier: metadata.identifier,
          issue_url: metadata.issue.url,
          state: metadata.issue.state,
          worker_host: Map.get(metadata, :worker_host),
          workspace_path: Map.get(metadata, :workspace_path),
          session_id: metadata.session_id,
          codex_app_server_pid: metadata.codex_app_server_pid,
          codex_input_tokens: metadata.codex_input_tokens,
          codex_output_tokens: metadata.codex_output_tokens,
          codex_total_tokens: metadata.codex_total_tokens,
          turn_count: Map.get(metadata, :turn_count, 0),
          started_at: metadata.started_at,
          last_codex_timestamp: metadata.last_codex_timestamp,
          last_progress_timestamp: Map.get(metadata, :last_progress_timestamp),
          last_codex_message: metadata.last_codex_message,
          last_codex_event: metadata.last_codex_event,
          selected_model_tier: Map.get(metadata, :selected_model_tier),
          actual_model: Map.get(metadata, :actual_model),
          routing_reason: Map.get(metadata, :routing_reason),
          escalated_from: Map.get(metadata, :escalated_from),
          escalation_history: Map.get(metadata, :escalation_history, []),
          runtime_seconds: running_seconds(metadata.started_at, now)
        }
      end)

    retrying =
      state.retry_attempts
      |> Enum.map(fn {issue_id, %{attempt: attempt, due_at_ms: due_at_ms} = retry} ->
        %{
          issue_id: issue_id,
          attempt: attempt,
          due_in_ms: max(0, due_at_ms - now_ms),
          identifier: Map.get(retry, :identifier),
          issue_url: Map.get(retry, :issue_url),
          error: Map.get(retry, :error),
          worker_host: Map.get(retry, :worker_host),
          workspace_path: Map.get(retry, :workspace_path),
          selected_model_tier: get_in(retry, [:model_route, :selected_tier]),
          actual_model: get_in(retry, [:model_route, :actual_model]),
          routing_reason: get_in(retry, [:model_route, :routing_reason]),
          escalated_from: get_in(retry, [:model_route, :escalated_from]),
          escalation_history: get_in(retry, [:model_route, :escalation_history]) || [],
          escalation_reason: Map.get(retry, :escalation_reason),
          delay_type: Map.get(retry, :delay_type),
          deferred_reason: Map.get(retry, :deferred_reason)
        }
      end)

    blocked =
      state.blocked
      |> Enum.map(fn {issue_id, metadata} ->
        %{
          issue_id: issue_id,
          identifier: Map.get(metadata, :identifier),
          issue_url: blocked_issue_url(metadata),
          state: blocked_issue_state(metadata),
          worker_host: Map.get(metadata, :worker_host),
          workspace_path: Map.get(metadata, :workspace_path),
          session_id: Map.get(metadata, :session_id),
          error: Map.get(metadata, :error),
          blocked_at: Map.get(metadata, :blocked_at),
          last_codex_timestamp: Map.get(metadata, :last_codex_timestamp),
          last_codex_message: Map.get(metadata, :last_codex_message),
          last_codex_event: Map.get(metadata, :last_codex_event),
          selected_model_tier: Map.get(metadata, :selected_model_tier),
          actual_model: Map.get(metadata, :actual_model),
          routing_reason: Map.get(metadata, :routing_reason),
          escalation_history: Map.get(metadata, :escalation_history, [])
        }
      end)

    {:reply,
     %{
       running: running,
       retrying: retrying,
       blocked: blocked,
       codex_totals: state.codex_totals,
       max_concurrent_agents: state.max_concurrent_agents,
       model_counts: model_counts(state),
       rate_limits: Map.get(state, :codex_rate_limits),
       issue_usage:
         issue_usage_snapshot(
           Map.get(state, :usage_ledger),
           Map.get(state, :codex_rate_limits),
           Map.get(state, :weekly_quota_observation)
         ),
       usage_aggregate:
         usage_aggregate_snapshot(
           Map.get(state, :usage_ledger),
           Map.get(state, :codex_rate_limits)
         ),
       source_circuit: SourceCircuit.snapshot(state.source_circuit, now_ms),
       polling: %{
         checking?: state.poll_check_in_progress == true,
         next_poll_in_ms: next_poll_in_ms(state.next_poll_due_at_ms, now_ms),
         poll_interval_ms: state.poll_interval_ms
       }
     }, state}
  end

  def handle_call(:request_refresh, _from, state) do
    now_ms = System.monotonic_time(:millisecond)
    already_due? = is_integer(state.next_poll_due_at_ms) and state.next_poll_due_at_ms <= now_ms
    coalesced = state.poll_check_in_progress == true or already_due?
    state = if coalesced, do: state, else: schedule_tick(state, 0)

    {:reply,
     %{
       queued: true,
       coalesced: coalesced,
       requested_at: DateTime.utc_now(),
       operations: ["poll", "reconcile"]
     }, state}
  end

  defp blocked_issue_state(%{issue: %Issue{state: state}}), do: state
  defp blocked_issue_state(_metadata), do: nil

  defp blocked_issue_url(%{issue: %Issue{url: url}}), do: url
  defp blocked_issue_url(_metadata), do: nil

  defp integrate_codex_update(running_entry, %{event: event, timestamp: timestamp} = update) do
    token_delta = extract_token_delta(running_entry, update)
    codex_input_tokens = Map.get(running_entry, :codex_input_tokens, 0)
    codex_output_tokens = Map.get(running_entry, :codex_output_tokens, 0)
    codex_total_tokens = Map.get(running_entry, :codex_total_tokens, 0)
    codex_app_server_pid = Map.get(running_entry, :codex_app_server_pid)
    last_reported_input = Map.get(running_entry, :codex_last_reported_input_tokens, 0)
    last_reported_output = Map.get(running_entry, :codex_last_reported_output_tokens, 0)
    last_reported_total = Map.get(running_entry, :codex_last_reported_total_tokens, 0)
    turn_count = Map.get(running_entry, :turn_count, 0)

    last_progress_timestamp =
      if codex_progress_update?(update, token_delta),
        do: timestamp,
        else: Map.get(running_entry, :last_progress_timestamp)

    {
      running_entry
      |> Map.merge(%{
        last_codex_timestamp: timestamp,
        last_progress_timestamp: last_progress_timestamp,
        last_codex_message: summarize_codex_update(update),
        session_id: session_id_for_update(running_entry.session_id, update),
        thread_id: thread_id_for_update(Map.get(running_entry, :thread_id), update),
        last_codex_event: event,
        codex_app_server_pid: codex_app_server_pid_for_update(codex_app_server_pid, update),
        codex_input_tokens: codex_input_tokens + token_delta.input_tokens,
        codex_output_tokens: codex_output_tokens + token_delta.output_tokens,
        codex_total_tokens: codex_total_tokens + token_delta.total_tokens,
        codex_last_reported_input_tokens: max(last_reported_input, token_delta.input_reported),
        codex_last_reported_output_tokens: max(last_reported_output, token_delta.output_reported),
        codex_last_reported_total_tokens: max(last_reported_total, token_delta.total_reported),
        turn_count: turn_count_for_update(turn_count, running_entry.session_id, update)
      })
      |> Map.merge(account_usage_for_update(update)),
      token_delta
    }
  end

  defp codex_progress_update?(_update, %{input_tokens: input, output_tokens: output, total_tokens: total})
       when input > 0 or output > 0 or total > 0,
       do: true

  defp codex_progress_update?(%{event: :notification, payload: payload}, _token_delta) do
    payload
    |> codex_message_method()
    |> progress_notification_method?()
  end

  defp codex_progress_update?(%{event: event}, _token_delta)
       when event in [
              :session_started,
              :turn_completed,
              :turn_failed,
              :turn_cancelled,
              :turn_input_required,
              :turn_ended_with_error,
              :approval_required,
              :approval_auto_approved,
              :tool_call_completed,
              :tool_call_failed,
              :unsupported_tool_call
            ],
       do: true

  defp codex_progress_update?(_update, _token_delta), do: false

  defp progress_notification_method?(method)
       when method in [
              "item/started",
              "item/updated",
              "item/completed",
              "item/agentMessage/delta",
              "item/agent_message/delta",
              "item/commandExecution/outputDelta",
              "item/commandExecution/requestApproval",
              "item/fileChange/outputDelta",
              "item/fileChange/requestApproval",
              "item/plan/delta",
              "item/reasoning/summaryPartAdded",
              "item/reasoning/summaryTextDelta",
              "item/reasoning/textDelta",
              "item/tool/call",
              "item/tool/requestUserInput",
              "turn/started",
              "turn/start",
              "turn/completed",
              "turn/failed",
              "turn/cancelled",
              "turn/diff/updated",
              "turn/plan/updated",
              "turn/input_required",
              "turn/need_input",
              "turn/needs_input",
              "turn/provide_input",
              "turn/request_input",
              "turn/request_response",
              "turn/approval_required"
            ],
       do: true

  defp progress_notification_method?(_method), do: false

  defp codex_app_server_pid_for_update(_existing, %{codex_app_server_pid: pid})
       when is_binary(pid),
       do: pid

  defp codex_app_server_pid_for_update(_existing, %{codex_app_server_pid: pid})
       when is_integer(pid),
       do: Integer.to_string(pid)

  defp codex_app_server_pid_for_update(_existing, %{codex_app_server_pid: pid}) when is_list(pid),
    do: to_string(pid)

  defp codex_app_server_pid_for_update(existing, _update), do: existing

  defp session_id_for_update(_existing, %{session_id: session_id}) when is_binary(session_id),
    do: session_id

  defp session_id_for_update(existing, _update), do: existing

  defp thread_id_for_update(_existing, %{thread_id: thread_id}) when is_binary(thread_id), do: thread_id

  defp thread_id_for_update(existing, _update), do: existing

  defp account_usage_for_update(update) do
    payload = update[:payload] || Map.get(update, "payload") || %{}
    thread_usage = account_usage_payload(payload)

    %{
      estimated_usage_credits_micros:
        map_integer_value(thread_usage, "estimatedUsageCreditsMicros") ||
          map_integer_value(thread_usage, :estimated_usage_credits_micros),
      estimated_usage_groups: Map.get(thread_usage, "groups") || Map.get(thread_usage, :groups)
    }
    |> Enum.reject(fn {_key, value} -> is_nil(value) end)
    |> Map.new()
  end

  defp account_usage_payload(payload) when is_map(payload) do
    case Map.get(payload, "threadUsage") || Map.get(payload, :thread_usage) || payload do
      usage when is_map(usage) -> usage
      _non_map_usage -> %{}
    end
  end

  defp account_usage_payload(_payload), do: %{}

  defp record_usage_completion(%State{usage_ledger: nil} = state, _issue_id, _running_entry), do: state

  defp record_usage_completion(%State{} = state, issue_id, running_entry) when is_map(running_entry) do
    case Map.get(running_entry, :thread_id) do
      thread_id when is_binary(thread_id) ->
        case UsageLedger.complete(state.usage_ledger, issue_id, thread_id, DateTime.utc_now()) do
          {:ok, ledger} ->
            %{state | usage_ledger: ledger}

          {:error, reason} ->
            Logger.warning("Failed to complete Codex usage issue_id=#{issue_id}: #{inspect(reason)}")
            state
        end

      _ ->
        state
    end
  end

  defp record_usage_completion(state, _issue_id, _running_entry), do: state

  defp record_usage_sample(%State{usage_ledger: nil} = state, _issue_id, _running_entry, _update), do: state

  defp record_usage_sample(%State{} = state, issue_id, running_entry, update) do
    usage = extract_full_token_usage(update, running_entry)
    thread_id = Map.get(running_entry, :thread_id)

    if is_binary(thread_id) and usage.total_tokens > 0 do
      entry = %{
        issue_id: issue_id,
        issue_identifier: Map.get(running_entry, :identifier),
        thread_id: thread_id,
        session_id: Map.get(running_entry, :session_id),
        model: Map.get(running_entry, :actual_model),
        tier: Map.get(running_entry, :selected_model_tier),
        started_at: Map.get(running_entry, :started_at),
        completed_at: nil,
        token_usage: usage,
        estimated_usage_credits_micros: Map.get(running_entry, :estimated_usage_credits_micros),
        estimated_usage_groups: Map.get(running_entry, :estimated_usage_groups)
      }

      case UsageLedger.record(state.usage_ledger, entry) do
        {:ok, ledger} ->
          %{state | usage_ledger: ledger}

        {:error, reason} ->
          Logger.warning("Failed to persist Codex usage issue_id=#{issue_id}: #{inspect(reason)}")
          state
      end
    else
      state
    end
  end

  defp extract_full_token_usage(update, running_entry) do
    usage = extract_token_usage(update)

    %{
      input_tokens: get_token_usage(usage, :input),
      cached_input_tokens: get_token_usage(usage, :cached_input),
      cache_write_input_tokens: get_token_usage(usage, :cache_write_input),
      output_tokens: get_token_usage(usage, :output),
      reasoning_output_tokens: get_token_usage(usage, :reasoning_output),
      total_tokens: get_token_usage(usage, :total)
    }
    |> Map.new(fn
      {:input_tokens, nil} -> {:input_tokens, Map.get(running_entry, :codex_input_tokens, 0)}
      {:output_tokens, nil} -> {:output_tokens, Map.get(running_entry, :codex_output_tokens, 0)}
      {:total_tokens, nil} -> {:total_tokens, Map.get(running_entry, :codex_total_tokens, 0)}
      {key, nil} -> {key, 0}
      {key, value} -> {key, value}
    end)
  end

  defp issue_usage_snapshot(nil, _rate_limits, _observation), do: %{}

  defp issue_usage_snapshot(ledger, rate_limits, observation) do
    entries = weekly_usage_entries(ledger, rate_limits)
    impacts = observed_week_impacts(entries, observation)

    entries
    |> Enum.group_by(& &1.issue_id)
    |> Map.new(&issue_usage_entry(&1, impacts))
  end

  defp observed_week_impacts(entries, %{observed_since: %DateTime{} = since, movement_percent: movement})
       when is_number(movement) and movement > 0 do
    observed_entries =
      Enum.filter(entries, fn entry ->
        case Map.get(entry, :started_at) do
          %DateTime{} = started -> DateTime.compare(started, since) in [:eq, :gt]
          _ -> false
        end
      end)

    UsageCost.approximate_week_impact(observed_entries, movement)
  end

  defp observed_week_impacts(_entries, _observation), do: %{}

  defp issue_usage_entry({issue_id, entries}, impacts) do
    current = Enum.max_by(entries, &usage_entry_recency/1)
    key = issue_usage_key(current.issue_identifier, issue_id)

    {key,
     %{
       issue_id: issue_id,
       issue_identifier: current.issue_identifier,
       current: current,
       aggregate: %{
         token_usage: aggregate_token_usage(entries),
         estimated_usage_credits_micros: aggregate_usage_credits(entries),
         week_impact_percent: Map.get(impacts, issue_id)
       }
     }}
  end

  defp aggregate_token_usage(entries) do
    Enum.reduce(entries, empty_usage_tokens(), fn entry, totals ->
      Map.merge(totals, entry.token_usage, fn _key, left, right -> left + right end)
    end)
  end

  defp aggregate_usage_credits(entries), do: Enum.reduce(entries, 0, &(&1.estimated_usage_credits_micros + &2))

  defp usage_aggregate_snapshot(nil, _rate_limits),
    do: %{token_usage: empty_usage_tokens(), estimated_usage_credits_micros: 0}

  defp usage_aggregate_snapshot(ledger, rate_limits) do
    entries = weekly_usage_entries(ledger, rate_limits)

    %{
      token_usage: aggregate_token_usage(entries),
      estimated_usage_credits_micros: aggregate_usage_credits(entries)
    }
  end

  defp weekly_usage_entries(ledger, rate_limits) do
    cutoff = weekly_window_start(rate_limits)

    ledger
    |> UsageLedger.snapshot()
    |> Map.fetch!(:current)
    |> Enum.filter(&usage_entry_in_window?(&1, cutoff))
  end

  defp usage_entry_in_window?(%{completed_at: nil}, _cutoff), do: true

  defp usage_entry_in_window?(entry, %DateTime{} = cutoff) do
    timestamp = Map.get(entry, :completed_at) || Map.get(entry, :started_at)

    case timestamp do
      %DateTime{} = value -> DateTime.compare(value, cutoff) in [:eq, :gt]
      _ -> true
    end
  end

  defp usage_entry_in_window?(_entry, _cutoff), do: true

  defp weekly_window_start(rate_limits) do
    bucket = weekly_rate_limit_bucket(rate_limits)
    reset_at = rate_limit_reset_at(bucket)
    duration_seconds = 10_080 * 60

    case reset_at do
      %DateTime{} = reset -> DateTime.add(reset, -duration_seconds)
      _ -> DateTime.add(DateTime.utc_now(), -duration_seconds)
    end
  end

  defp weekly_used_percent(rate_limits) do
    bucket = weekly_rate_limit_bucket(rate_limits)

    case map_numeric_value(bucket || %{}, ["usedPercent", :usedPercent, "used_percent", :used_percent]) do
      value when is_number(value) -> value
      _ -> nil
    end
  end

  defp weekly_rate_limit_bucket(value) when is_map(value) do
    duration =
      map_integer_value(value, "windowDurationMins") || map_integer_value(value, :windowDurationMins) ||
        map_integer_value(value, "window_duration_mins") || map_integer_value(value, :window_duration_mins)

    if duration == 10_080 do
      value
    else
      Enum.find_value(Map.values(value), &weekly_rate_limit_bucket/1)
    end
  end

  defp weekly_rate_limit_bucket(value) when is_list(value),
    do: Enum.find_value(value, &weekly_rate_limit_bucket/1)

  defp weekly_rate_limit_bucket(_value), do: nil

  defp rate_limit_reset_at(bucket) when is_map(bucket) do
    value =
      Map.get(bucket, "resetsAt") || Map.get(bucket, :resetsAt) ||
        Map.get(bucket, "resets_at") || Map.get(bucket, :resets_at)

    cond do
      is_integer(value) ->
        DateTime.from_unix!(value)

      is_binary(value) ->
        case DateTime.from_iso8601(value) do
          {:ok, datetime, _offset} -> datetime
          _ -> nil
        end

      true ->
        nil
    end
  end

  defp rate_limit_reset_at(_bucket), do: nil

  defp map_numeric_value(map, keys) when is_map(map) and is_list(keys) do
    Enum.find_value(keys, fn key ->
      case Map.get(map, key) do
        value when is_number(value) -> value
        _ -> nil
      end
    end)
  end

  defp empty_usage_tokens do
    %{
      input_tokens: 0,
      cached_input_tokens: 0,
      cache_write_input_tokens: 0,
      output_tokens: 0,
      reasoning_output_tokens: 0,
      total_tokens: 0
    }
  end

  defp issue_usage_key(identifier, _issue_id) when is_binary(identifier) do
    case Regex.run(~r/(\d+)$/, identifier) do
      [_, number] -> number
      _ -> identifier
    end
  end

  defp issue_usage_key(_identifier, issue_id), do: issue_id

  defp usage_entry_recency(entry) do
    timestamp = Map.get(entry, :started_at) || Map.get(entry, :completed_at)

    unix_micros =
      case timestamp do
        %DateTime{} = datetime -> DateTime.to_unix(datetime, :microsecond)
        _ -> 0
      end

    {unix_micros, Map.get(entry, :thread_id, "")}
  end

  defp usage_ledger_path(_config) do
    System.get_env("SYMPHONY_USAGE_LEDGER_PATH") || Path.join(Config.local_workspace_root(), "symphony-usage.jsonl")
  end

  defp turn_count_for_update(existing_count, existing_session_id, %{
         event: :session_started,
         session_id: session_id
       })
       when is_integer(existing_count) and is_binary(session_id) do
    if session_id == existing_session_id do
      existing_count
    else
      existing_count + 1
    end
  end

  defp turn_count_for_update(existing_count, _existing_session_id, _update)
       when is_integer(existing_count),
       do: existing_count

  defp turn_count_for_update(_existing_count, _existing_session_id, _update), do: 0

  defp summarize_codex_update(update) do
    %{
      event: update[:event],
      message: update[:payload] || update[:raw],
      timestamp: update[:timestamp]
    }
  end

  defp schedule_tick(%State{} = state, delay_ms) when is_integer(delay_ms) and delay_ms >= 0 do
    if is_reference(state.tick_timer_ref) do
      Process.cancel_timer(state.tick_timer_ref)
    end

    tick_token = make_ref()
    timer_ref = Process.send_after(self(), {:tick, tick_token}, delay_ms)

    %{
      state
      | tick_timer_ref: timer_ref,
        tick_token: tick_token,
        next_poll_due_at_ms: System.monotonic_time(:millisecond) + delay_ms
    }
  end

  defp schedule_poll_cycle_start do
    :timer.send_after(@poll_transition_render_delay_ms, self(), :run_poll_cycle)
    :ok
  end

  defp next_poll_in_ms(nil, _now_ms), do: nil

  defp next_poll_in_ms(next_poll_due_at_ms, now_ms) when is_integer(next_poll_due_at_ms) do
    max(0, next_poll_due_at_ms - now_ms)
  end

  defp pop_running_entry(state, issue_id) do
    {Map.get(state.running, issue_id), %{state | running: Map.delete(state.running, issue_id)}}
  end

  defp record_session_completion_totals(state, running_entry) when is_map(running_entry) do
    runtime_seconds = running_seconds(running_entry.started_at, DateTime.utc_now())

    codex_totals =
      apply_token_delta(
        state.codex_totals,
        %{
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
          seconds_running: runtime_seconds
        }
      )

    %{state | codex_totals: codex_totals}
  end

  defp record_session_completion_totals(state, _running_entry), do: state

  defp record_model_completion(%State{} = state, running_entry) when is_map(running_entry) do
    case Map.get(running_entry, :selected_model_tier) do
      tier when tier in [:luna, :terra, :sol] ->
        counts = Map.update(state.model_completed_counts, tier, 1, &(&1 + 1))
        %{state | model_completed_counts: counts}

      _ ->
        state
    end
  end

  defp record_model_completion(state, _running_entry), do: state

  defp model_counts(%State{} = state) do
    Enum.into([:luna, :terra, :sol], %{}, fn tier ->
      active =
        Enum.count(state.running, fn {_issue_id, entry} ->
          Map.get(entry, :selected_model_tier) == tier
        end)

      {tier, %{active: active, completed: Map.get(state.model_completed_counts, tier, 0)}}
    end)
  end

  defp refresh_runtime_config(%State{} = state) do
    config = Config.settings!()

    %{
      state
      | poll_interval_ms: config.polling.interval_ms,
        max_concurrent_agents: config.agent.max_concurrent_agents
    }
  end

  defp retry_candidate_issue?(%Issue{} = issue, terminal_states) do
    candidate_issue?(
      issue,
      active_state_set(),
      terminal_states,
      :owner_control_disabled,
      :owner_control_disabled
    )
  end

  defp dispatch_slots_available?(%Issue{} = issue, %State{} = state) do
    available_slots(state) > 0 and state_slots_available?(issue, state.running)
  end

  defp apply_codex_token_delta(
         %{codex_totals: codex_totals} = state,
         %{input_tokens: input, output_tokens: output, total_tokens: total} = token_delta
       )
       when is_integer(input) and is_integer(output) and is_integer(total) do
    %{state | codex_totals: apply_token_delta(codex_totals, token_delta)}
  end

  defp apply_codex_token_delta(state, _token_delta), do: state

  defp start_account_rate_limit_refresh(%State{account_rate_limit_refresh_in_flight: true} = state),
    do: state

  defp start_account_rate_limit_refresh(%State{} = state) do
    recipient = self()
    reader = state.account_rate_limits_reader

    task = fn ->
      result =
        try do
          reader.()
        rescue
          exception -> {:error, {:account_rate_limits_failed, Exception.message(exception)}}
        catch
          kind, reason -> {:error, {:account_rate_limits_failed, {kind, reason}}}
        end

      send(recipient, {:account_rate_limits_result, result})
    end

    case Task.Supervisor.start_child(state.task_supervisor, task) do
      {:ok, _pid} ->
        %{state | account_rate_limit_refresh_in_flight: true}

      {:error, reason} ->
        Logger.warning("Failed to start Codex account rate-limit refresh: #{inspect(reason)}")
        state
    end
  end

  defp apply_codex_rate_limits(%State{} = state, update) when is_map(update) do
    case extract_rate_limits(update) do
      %{} = rate_limits ->
        merged = merge_rate_limits(state.codex_rate_limits, rate_limits)

        %{
          state
          | codex_rate_limits: merged,
            weekly_quota_observation: update_weekly_quota_observation(state, merged, update)
        }

      _ ->
        state
    end
  end

  defp apply_codex_rate_limits(state, _update), do: state

  defp update_weekly_quota_observation(state, rate_limits, update) do
    used = weekly_used_percent(rate_limits)
    reset_at = rate_limits |> weekly_rate_limit_bucket() |> rate_limit_reset_at()
    observed_at = Map.get(update, :timestamp) || Map.get(update, "timestamp") || DateTime.utc_now()
    current = Map.get(state, :weekly_quota_observation)

    cond do
      not is_number(used) ->
        current

      not match?(%DateTime{}, observed_at) ->
        current

      not is_map(current) or current.reset_at != reset_at or used < current.baseline_used_percent ->
        %{
          reset_at: reset_at,
          observed_since: observed_at,
          baseline_used_percent: used,
          current_used_percent: used,
          movement_percent: 0.0
        }

      true ->
        %{
          current
          | current_used_percent: used,
            movement_percent: max(used - current.baseline_used_percent, 0.0)
        }
    end
  end

  defp merge_rate_limits(nil, rate_limits), do: rate_limits

  defp merge_rate_limits(existing, incoming) when is_map(existing) and is_map(incoming) do
    Map.merge(existing, incoming, fn _key, left, right ->
      if is_map(left) and is_map(right), do: merge_rate_limits(left, right), else: right
    end)
  end

  defp apply_token_delta(codex_totals, token_delta) do
    input_tokens = Map.get(codex_totals, :input_tokens, 0) + token_delta.input_tokens
    output_tokens = Map.get(codex_totals, :output_tokens, 0) + token_delta.output_tokens
    total_tokens = Map.get(codex_totals, :total_tokens, 0) + token_delta.total_tokens

    seconds_running =
      Map.get(codex_totals, :seconds_running, 0) + Map.get(token_delta, :seconds_running, 0)

    %{
      input_tokens: max(0, input_tokens),
      output_tokens: max(0, output_tokens),
      total_tokens: max(0, total_tokens),
      seconds_running: max(0, seconds_running)
    }
  end

  defp extract_token_delta(running_entry, %{event: _, timestamp: _} = update) do
    running_entry = running_entry || %{}
    usage = extract_token_usage(update)

    {
      compute_token_delta(
        running_entry,
        :input,
        usage,
        :codex_last_reported_input_tokens
      ),
      compute_token_delta(
        running_entry,
        :output,
        usage,
        :codex_last_reported_output_tokens
      ),
      compute_token_delta(
        running_entry,
        :total,
        usage,
        :codex_last_reported_total_tokens
      )
    }
    |> Tuple.to_list()
    |> then(fn [input, output, total] ->
      %{
        input_tokens: input.delta,
        output_tokens: output.delta,
        total_tokens: total.delta,
        input_reported: input.reported,
        output_reported: output.reported,
        total_reported: total.reported
      }
    end)
  end

  defp compute_token_delta(running_entry, token_key, usage, reported_key) do
    next_total = get_token_usage(usage, token_key)
    prev_reported = Map.get(running_entry, reported_key, 0)

    delta =
      if is_integer(next_total) and next_total >= prev_reported do
        next_total - prev_reported
      else
        0
      end

    %{
      delta: max(delta, 0),
      reported: if(is_integer(next_total), do: next_total, else: prev_reported)
    }
  end

  defp extract_token_usage(update) do
    payloads = [
      update[:usage],
      Map.get(update, "usage"),
      Map.get(update, :usage),
      update[:payload],
      Map.get(update, "payload"),
      update
    ]

    Enum.find_value(payloads, &absolute_token_usage_from_payload/1) ||
      Enum.find_value(payloads, &turn_completed_usage_from_payload/1) ||
      %{}
  end

  defp extract_rate_limits(update) do
    rate_limits_from_payload(update[:rate_limits]) ||
      rate_limits_from_payload(Map.get(update, "rate_limits")) ||
      rate_limits_from_payload(Map.get(update, :rate_limits)) ||
      rate_limits_from_payload(update[:payload]) ||
      rate_limits_from_payload(Map.get(update, "payload")) ||
      rate_limits_from_payload(update)
  end

  defp absolute_token_usage_from_payload(payload) when is_map(payload) do
    absolute_paths = [
      ["params", "msg", "payload", "info", "total_token_usage"],
      [:params, :msg, :payload, :info, :total_token_usage],
      ["params", "msg", "info", "total_token_usage"],
      [:params, :msg, :info, :total_token_usage],
      ["params", "tokenUsage", "total"],
      [:params, :tokenUsage, :total],
      ["tokenUsage", "total"],
      [:tokenUsage, :total]
    ]

    explicit_map_at_paths(payload, absolute_paths)
  end

  defp absolute_token_usage_from_payload(_payload), do: nil

  defp turn_completed_usage_from_payload(payload) when is_map(payload) do
    method = Map.get(payload, "method") || Map.get(payload, :method)

    if method in ["turn/completed", :turn_completed] do
      direct =
        Map.get(payload, "usage") ||
          Map.get(payload, :usage) ||
          map_at_path(payload, ["params", "usage"]) ||
          map_at_path(payload, [:params, :usage])

      if is_map(direct) and integer_token_map?(direct), do: direct
    end
  end

  defp turn_completed_usage_from_payload(_payload), do: nil

  defp rate_limits_from_payload(payload) when is_map(payload) do
    direct =
      Map.get(payload, "rate_limits") || Map.get(payload, :rate_limits) ||
        Map.get(payload, "rateLimits") || Map.get(payload, :rateLimits)

    cond do
      rate_limits_map?(direct) ->
        direct

      rate_limits_map?(payload) ->
        payload

      true ->
        rate_limit_payloads(payload)
    end
  end

  defp rate_limits_from_payload(payload) when is_list(payload) do
    rate_limit_payloads(payload)
  end

  defp rate_limits_from_payload(_payload), do: nil

  defp rate_limit_payloads(payload) when is_map(payload) do
    Map.values(payload)
    |> Enum.reduce_while(nil, fn
      value, nil ->
        case rate_limits_from_payload(value) do
          nil -> {:cont, nil}
          rate_limits -> {:halt, rate_limits}
        end

      _value, result ->
        {:halt, result}
    end)
  end

  defp rate_limit_payloads(payload) when is_list(payload) do
    payload
    |> Enum.reduce_while(nil, fn
      value, nil ->
        case rate_limits_from_payload(value) do
          nil -> {:cont, nil}
          rate_limits -> {:halt, rate_limits}
        end

      _value, result ->
        {:halt, result}
    end)
  end

  defp rate_limits_map?(payload) when is_map(payload) do
    Enum.any?(["primary", :primary, "secondary", :secondary], fn key ->
      case Map.get(payload, key) do
        bucket when is_map(bucket) -> rate_limit_bucket?(bucket)
        _ -> false
      end
    end)
  end

  defp rate_limits_map?(_payload), do: false

  defp rate_limit_bucket?(bucket) do
    Enum.any?(
      [
        "usedPercent",
        :usedPercent,
        "used_percent",
        :used_percent,
        "windowDurationMins",
        :windowDurationMins,
        "window_duration_mins",
        :window_duration_mins,
        "remaining",
        :remaining,
        "limit",
        :limit
      ],
      &Map.has_key?(bucket, &1)
    )
  end

  defp explicit_map_at_paths(payload, paths) when is_map(payload) and is_list(paths) do
    Enum.find_value(paths, fn path ->
      value = map_at_path(payload, path)

      if is_map(value) and integer_token_map?(value), do: value
    end)
  end

  defp explicit_map_at_paths(_payload, _paths), do: nil

  defp map_at_path(payload, path) when is_map(payload) and is_list(path) do
    Enum.reduce_while(path, payload, fn key, acc ->
      if is_map(acc) and Map.has_key?(acc, key) do
        {:cont, Map.get(acc, key)}
      else
        {:halt, nil}
      end
    end)
  end

  defp map_at_path(_payload, _path), do: nil

  defp integer_token_map?(payload) do
    token_fields = [
      :input_tokens,
      :output_tokens,
      :total_tokens,
      :prompt_tokens,
      :completion_tokens,
      :inputTokens,
      :outputTokens,
      :totalTokens,
      :promptTokens,
      :completionTokens,
      "input_tokens",
      "output_tokens",
      "total_tokens",
      "prompt_tokens",
      "completion_tokens",
      "inputTokens",
      "outputTokens",
      "totalTokens",
      "promptTokens",
      "completionTokens"
    ]

    token_fields
    |> Enum.any?(fn field ->
      value = payload_get(payload, field)
      !is_nil(integer_like(value))
    end)
  end

  defp get_token_usage(usage, :input),
    do:
      payload_get(usage, [
        "input_tokens",
        "prompt_tokens",
        :input_tokens,
        :prompt_tokens,
        :input,
        "promptTokens",
        :promptTokens,
        "inputTokens",
        :inputTokens
      ])

  defp get_token_usage(usage, :output),
    do:
      payload_get(usage, [
        "output_tokens",
        "completion_tokens",
        :output_tokens,
        :completion_tokens,
        :output,
        :completion,
        "outputTokens",
        :outputTokens,
        "completionTokens",
        :completionTokens
      ])

  defp get_token_usage(usage, :cached_input),
    do:
      payload_get(usage, [
        "cached_input_tokens",
        :cached_input_tokens,
        "cachedInputTokens",
        :cachedInputTokens
      ])

  defp get_token_usage(usage, :cache_write_input),
    do:
      payload_get(usage, [
        "cache_write_input_tokens",
        :cache_write_input_tokens,
        "cacheWriteInputTokens",
        :cacheWriteInputTokens
      ])

  defp get_token_usage(usage, :reasoning_output),
    do:
      payload_get(usage, [
        "reasoning_output_tokens",
        :reasoning_output_tokens,
        "reasoningOutputTokens",
        :reasoningOutputTokens
      ])

  defp get_token_usage(usage, :total),
    do:
      payload_get(usage, [
        "total_tokens",
        "total",
        :total_tokens,
        :total,
        "totalTokens",
        :totalTokens
      ])

  defp payload_get(payload, fields) when is_list(fields) do
    Enum.find_value(fields, fn field -> map_integer_value(payload, field) end)
  end

  defp payload_get(payload, field), do: map_integer_value(payload, field)

  defp map_integer_value(payload, field) do
    if is_map(payload) do
      value = Map.get(payload, field)
      integer_like(value)
    else
      nil
    end
  end

  defp running_seconds(%DateTime{} = started_at, %DateTime{} = now) do
    max(0, DateTime.diff(now, started_at, :second))
  end

  defp running_seconds(_started_at, _now), do: 0

  defp integer_like(value) when is_integer(value) and value >= 0, do: value

  defp integer_like(value) when is_binary(value) do
    case Integer.parse(String.trim(value)) do
      {num, _} when num >= 0 -> num
      _ -> nil
    end
  end

  defp integer_like(_value), do: nil
end
