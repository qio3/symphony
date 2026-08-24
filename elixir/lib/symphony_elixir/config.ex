defmodule SymphonyElixir.Config do
  @moduledoc """
  Runtime configuration loaded from `WORKFLOW.md`.
  """

  alias SymphonyElixir.{Config.Schema, Tracker}
  alias SymphonyElixir.{Workflow, WorkflowStore}

  @settings_snapshot_key {__MODULE__, :settings_snapshot}

  @default_prompt_template """
  You are working on an issue from the configured tracker.

  Identifier: {{ issue.identifier }}
  Title: {{ issue.title }}

  Body:
  {% if issue.description %}
  {{ issue.description }}
  {% else %}
  No description provided.
  {% endif %}
  """

  @type codex_runtime_settings :: %{
          approval_policy: String.t() | map(),
          thread_sandbox: String.t(),
          turn_sandbox_policy: map()
        }

  @spec settings() :: {:ok, Schema.t()} | {:error, term()}
  def settings do
    case Process.get(@settings_snapshot_key, :unset) do
      :unset -> WorkflowStore.settings()
      settings -> {:ok, settings}
    end
  end

  @doc false
  @spec loaded_settings_snapshot() ::
          {:ok, %{settings: Schema.t(), workflow_path: Path.t()}} | {:error, term()}
  def loaded_settings_snapshot do
    WorkflowStore.settings_snapshot()
  end

  @doc false
  @spec with_settings_snapshot(Schema.t(), (-> result)) :: result when result: term()
  def with_settings_snapshot(settings, fun) when is_function(fun, 0) do
    previous = Process.get(@settings_snapshot_key, :unset)
    Process.put(@settings_snapshot_key, settings)

    try do
      fun.()
    after
      restore_settings_snapshot(previous)
    end
  end

  @spec settings!() :: Schema.t()
  def settings! do
    case settings() do
      {:ok, settings} ->
        settings

      {:error, reason} ->
        raise ArgumentError, message: format_config_error(reason)
    end
  end

  @spec max_concurrent_agents_for_state(term()) :: pos_integer()
  def max_concurrent_agents_for_state(state_name) when is_binary(state_name) do
    config = settings!()

    Map.get(
      config.agent.max_concurrent_agents_by_state,
      Schema.normalize_issue_state(state_name),
      config.agent.max_concurrent_agents
    )
  end

  def max_concurrent_agents_for_state(_state_name), do: settings!().agent.max_concurrent_agents

  @spec codex_turn_sandbox_policy(Path.t() | nil) :: map()
  def codex_turn_sandbox_policy(workspace \\ nil) do
    case Schema.resolve_runtime_turn_sandbox_policy(settings!(), workspace) do
      {:ok, policy} ->
        policy

      {:error, reason} ->
        raise ArgumentError, message: "Invalid codex turn sandbox policy: #{inspect(reason)}"
    end
  end

  @spec workflow_prompt() :: String.t()
  def workflow_prompt do
    case Workflow.current() do
      {:ok, %{prompt_template: prompt}} ->
        if String.trim(prompt) == "", do: @default_prompt_template, else: prompt

      _ ->
        @default_prompt_template
    end
  end

  @spec server_port() :: non_neg_integer() | nil
  def server_port do
    case Application.get_env(:symphony_elixir, :server_port_override) do
      port when is_integer(port) and port >= 0 -> port
      _ -> settings!().server.port
    end
  end

  @type owner_control_settings :: %{url: String.t(), token: String.t()}

  @spec owner_control_settings() :: :disabled | {:ok, owner_control_settings()} | {:error, term()}
  def owner_control_settings do
    case Application.get_env(:symphony_elixir, :owner_control_settings) do
      nil ->
        owner_control_settings_from_env(
          System.get_env("SYMPHONY_OWNER_CONTROL_URL"),
          System.get_env("SYMPHONY_OWNER_CONTROL_TOKEN")
        )

      settings when is_map(settings) ->
        validate_owner_control_settings(settings)

      _other ->
        {:error, :invalid_owner_control_settings}
    end
  end

  @doc false
  @spec local_workspace_root() :: Path.t()
  def local_workspace_root do
    workflow_dir = Workflow.workflow_file_path() |> Path.expand() |> Path.dirname()
    Path.expand(settings!().workspace.root, workflow_dir)
  end

  @spec validate!() :: :ok | {:error, term()}
  def validate! do
    WorkflowStore.force_reload()
  end

  @spec codex_runtime_settings(Path.t() | nil, keyword()) ::
          {:ok, codex_runtime_settings()} | {:error, term()}
  def codex_runtime_settings(workspace \\ nil, opts \\ []) do
    with {:ok, settings} <- settings() do
      with {:ok, turn_sandbox_policy} <-
             Schema.resolve_runtime_turn_sandbox_policy(settings, workspace, opts) do
        {:ok,
         %{
           approval_policy: settings.codex.approval_policy,
           thread_sandbox: settings.codex.thread_sandbox,
           turn_sandbox_policy: turn_sandbox_policy
         }}
      end
    end
  end

  @doc false
  @spec validate_settings(Schema.t()) :: :ok | {:error, term()}
  def validate_settings(settings) do
    if is_nil(settings.tracker.kind) do
      {:error, :missing_tracker_kind}
    else
      Tracker.validate_config(settings.tracker)
    end
  end

  defp format_config_error(reason) do
    case reason do
      {:invalid_workflow_config, message} ->
        "Invalid WORKFLOW.md config: #{message}"

      {:missing_workflow_file, path, raw_reason} ->
        "Missing WORKFLOW.md at #{path}: #{inspect(raw_reason)}"

      {:workflow_parse_error, raw_reason} ->
        "Failed to parse WORKFLOW.md: #{inspect(raw_reason)}"

      :workflow_front_matter_not_a_map ->
        "Failed to parse WORKFLOW.md: workflow front matter must decode to a map"

      other ->
        "Invalid WORKFLOW.md config: #{inspect(other)}"
    end
  end

  defp restore_settings_snapshot(:unset), do: Process.delete(@settings_snapshot_key)
  defp restore_settings_snapshot(settings), do: Process.put(@settings_snapshot_key, settings)

  defp owner_control_settings_from_env(nil, nil), do: :disabled

  defp owner_control_settings_from_env(url, token) do
    validate_owner_control_settings(%{url: url, token: token})
  end

  defp validate_owner_control_settings(settings) do
    url = Map.get(settings, :url) || Map.get(settings, "url")
    token = Map.get(settings, :token) || Map.get(settings, "token")

    with {:ok, normalized_url} <- normalize_owner_control_url(url),
         :ok <- validate_owner_control_token(token) do
      {:ok, %{url: normalized_url, token: token}}
    end
  end

  defp normalize_owner_control_url(url) when is_binary(url) do
    case URI.parse(url) do
      %URI{scheme: scheme, host: host}
      when scheme in ["http", "https"] and is_binary(host) and host != "" ->
        {:ok, String.trim_trailing(url, "/")}

      _other ->
        {:error, :invalid_owner_control_settings}
    end
  end

  defp normalize_owner_control_url(_url), do: {:error, :invalid_owner_control_settings}

  defp validate_owner_control_token(token) when is_binary(token) and byte_size(token) >= 32,
    do: :ok

  defp validate_owner_control_token(_token), do: {:error, :invalid_owner_control_settings}
end
