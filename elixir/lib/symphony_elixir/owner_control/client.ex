defmodule SymphonyElixir.OwnerControl.Client do
  @moduledoc """
  Fail-closed client for the external deterministic owner-control process.
  """

  alias SymphonyElixir.Config

  @actions ~w(run accept rework pause resume start_service stop_service restart)a
  @known_keys ~w(
    version generated_at service live reason container status started_at restart_count
    intake active workers running limit counts backlog ready_for_ai queued retrying blocked
    ready_for_acceptance done canonical test sha url ref synced drift issues owner_view available
    updated_at work_items codex_totals input_tokens output_tokens total_tokens seconds_running
    rate_limits quota five_hour weekly used_percent window_duration_mins resets_at failures
    unrecoverable fingerprint message number identifier issue_identifier issue_usage usage
    aggregate estimated_credits_micros estimated_usage_credits_micros cached_input_tokens
    cache_write_input_tokens reasoning_output_tokens completed_at thread_id sources stale
    refreshed_at supervisor runtime github fresh unknown confirmed_at max_concurrent_agents
    issue_id issue_url title stage state labels owner_question question project_item_id pr ci
    merged due_at last_message last_event last_event_at error attempt worker_host workspace_path
    session_id turn_count tokens health_url models model selected_tier actual_model routing_reason
    escalated_from escalation_history luna terra sol completed from to
  )a
  @key_lookup Map.new(@known_keys, &{Atom.to_string(&1), &1})

  @spec enabled?() :: boolean()
  def enabled? do
    Config.owner_control_settings() != :disabled
  end

  @spec snapshot() :: {:ok, map()} | {:error, term()} | :disabled
  def snapshot do
    case Config.owner_control_settings() do
      :disabled ->
        :disabled

      {:ok, settings} ->
        request(:get, settings, "/v1/snapshot", nil)

      {:error, reason} ->
        {:error, reason}
    end
  end

  @spec action(atom(), map()) :: {:ok, map()} | {:error, term()}
  def action(action, params) when action in @actions and is_map(params) do
    case Config.owner_control_settings() do
      {:ok, settings} -> request(:post, settings, "/v1/actions/#{action}", params)
      :disabled -> {:error, :owner_control_disabled}
      {:error, reason} -> {:error, reason}
    end
  end

  def action(_action, _params), do: {:error, :unsupported_action}

  @spec intake_active?() :: boolean()
  def intake_active? do
    case Config.owner_control_settings() do
      :disabled ->
        true

      {:ok, settings} ->
        case request(:get, settings, "/v1/intake", nil) do
          {:ok, %{active: active}} when is_boolean(active) -> active
          _other -> false
        end

      {:error, _reason} ->
        false
    end
  end

  defp request(method, settings, path, body) do
    headers = [
      {"authorization", "Bearer #{settings.token}"},
      {"accept", "application/json"}
    ]

    request_fun =
      Application.get_env(
        :symphony_elixir,
        :owner_control_request_fun,
        &default_request/4
      )

    case request_fun.(method, settings.url <> path, headers, body) do
      {:ok, status, response_body} when status in 200..299 -> decode_body(response_body)
      {:ok, status, response_body} -> decode_http_error(status, response_body)
      {:error, reason} -> {:error, {:owner_control_unavailable, reason}}
    end
  rescue
    exception -> {:error, {:owner_control_request_failed, Exception.message(exception)}}
  end

  defp default_request(method, url, headers, body) do
    options = [
      method: method,
      url: url,
      headers: headers,
      receive_timeout: 3_000,
      connect_options: [timeout: 1_000],
      retry: false
    ]

    options = if is_nil(body), do: options, else: Keyword.put(options, :json, body)

    case Req.request(options) do
      {:ok, %Req.Response{status: status, body: response_body}} -> {:ok, status, response_body}
      {:error, reason} -> {:error, reason}
    end
  end

  defp decode_body(body) when is_map(body), do: {:ok, normalize_keys(body)}

  defp decode_body(body) when is_binary(body) do
    case Jason.decode(body) do
      {:ok, decoded} when is_map(decoded) -> {:ok, normalize_keys(decoded)}
      {:ok, _decoded} -> {:error, :invalid_owner_control_payload}
      {:error, reason} -> {:error, {:invalid_owner_control_json, reason}}
    end
  end

  defp decode_body(_body), do: {:error, :invalid_owner_control_payload}

  defp decode_http_error(status, body) when status in [409, 503] do
    case decode_error_message(body) do
      {:ok, message} -> {:error, {:owner_control_action_rejected, message}}
      :error -> {:error, {:owner_control_http_error, status}}
    end
  end

  defp decode_http_error(status, _body), do: {:error, {:owner_control_http_error, status}}

  defp decode_error_message(body) when is_binary(body) do
    case Jason.decode(body) do
      {:ok, decoded} -> decode_error_message(decoded)
      _error -> :error
    end
  end

  defp decode_error_message(%{"error" => %{"message" => message}}) when is_binary(message),
    do: {:ok, message}

  defp decode_error_message(%{error: %{message: message}}) when is_binary(message),
    do: {:ok, message}

  defp decode_error_message(_body), do: :error

  defp normalize_keys(value) when is_list(value), do: Enum.map(value, &normalize_keys/1)

  defp normalize_keys(value) when is_map(value) do
    Map.new(value, fn {key, nested} ->
      normalized_key = if is_binary(key), do: Map.get(@key_lookup, key, key), else: key
      {normalized_key, normalize_keys(nested)}
    end)
  end

  defp normalize_keys(value), do: value
end
