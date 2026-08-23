defmodule SymphonyElixir.Codex.MetadataClassifier do
  @moduledoc """
  Runs a short, tool-free Luna turn over issue metadata only.
  """

  alias SymphonyElixir.Codex.AppServer
  alias SymphonyElixir.Config
  alias SymphonyElixir.Tracker.Issue

  @spec classify(map()) :: map()
  def classify(metadata) when is_map(metadata) do
    settings = Config.settings!().model_routing
    workspace = Path.join(Config.local_workspace_root(), ".model-classifier")
    File.mkdir_p!(workspace)
    messages_key = {__MODULE__, make_ref()}
    Process.put(messages_key, [])

    issue = %Issue{
      id: "model-classifier",
      identifier: "MODEL-ROUTER",
      title: to_string(Map.get(metadata, :title, "Issue classification"))
    }

    on_message = fn message ->
      Process.put(messages_key, [message | Process.get(messages_key, [])])
    end

    try do
      with {:ok, _result} <-
             AppServer.run(workspace, prompt(metadata), issue,
               model: settings.classifier_model,
               dynamic_tools: false,
               turn_timeout_ms: settings.timeout_ms,
               on_message: on_message
             ),
           output when is_binary(output) <- messages_key |> Process.get([]) |> classifier_output(),
           {:ok, result} <- parse_output(output) do
        result
      else
        error -> raise "model classifier failed: #{inspect(error)}"
      end
    after
      Process.delete(messages_key)
    end
  end

  @doc false
  @spec prompt_for_test(map()) :: String.t()
  def prompt_for_test(metadata), do: prompt(metadata)

  @doc false
  @spec parse_output_for_test(String.t()) :: {:ok, map()} | {:error, term()}
  def parse_output_for_test(output), do: parse_output(output)

  defp prompt(metadata) do
    """
    Classify the engineering issue below using only the supplied JSON metadata.
    Do not call tools, read files, run commands, or obtain any additional context.

    Return exactly one JSON object with this schema:
    {"tier":"luna|terra|sol","confidence":0.0,"reason":"short_machine_reason"}

    luna: bounded/localized low-risk bug, test, UI copy/layout, config, docs, or mechanical one-subsystem change.
    terra: normal default development, feature/debugging/integration, several files, several edge cases, or moderate cross-component work.
    sol: architecture, difficult unknown root cause, concurrency/race, security/auth/access, destructive data integrity, complex migration, or difficult infrastructure/release reasoning.
    Do not choose sol merely because words such as CI, backend, or migration appear. If uncertain, return terra with confidence below 0.65.

    ISSUE_METADATA_JSON:
    #{Jason.encode!(metadata)}
    """
  end

  defp classifier_output(messages) when is_list(messages) do
    messages = Enum.reverse(messages)

    deltas =
      Enum.flat_map(messages, fn message ->
        payload = Map.get(message, :payload, %{})
        method = Map.get(payload, "method")
        delta = get_in(payload, ["params", "delta"])

        if method in ["item/agentMessage/delta", "item/agent_message/delta"] and is_binary(delta),
          do: [delta],
          else: []
      end)

    case deltas do
      [_ | _] -> Enum.join(deltas)
      [] -> messages |> Enum.flat_map(&agent_texts/1) |> List.last()
    end
  end

  defp agent_texts(%{payload: payload}), do: agent_texts(payload)

  defp agent_texts(%{} = map) do
    own =
      if Map.get(map, "type") in ["agentMessage", "agent_message"] do
        [Map.get(map, "text") || Map.get(map, "content")]
      else
        []
      end

    Enum.filter(own, &is_binary/1) ++ Enum.flat_map(Map.values(map), &agent_texts/1)
  end

  defp agent_texts(list) when is_list(list), do: Enum.flat_map(list, &agent_texts/1)
  defp agent_texts(value) when is_binary(value), do: []
  defp agent_texts(_value), do: []

  defp parse_output(output) when is_binary(output) do
    with [json] <- Regex.run(~r/\{.*\}/s, output),
         {:ok, decoded} <- Jason.decode(json),
         %{"tier" => tier, "confidence" => confidence, "reason" => reason} <- decoded,
         true <- is_binary(tier) and is_number(confidence) and is_binary(reason) do
      {:ok, %{tier: tier, confidence: confidence / 1.0, reason: reason}}
    else
      error -> {:error, {:invalid_classifier_output, error}}
    end
  end
end
