defmodule SymphonyElixir.BlockedReview do
  @moduledoc """
  Runs one bounded, read-only GPT-6 Astra review for a durable Blocked version.

  The model can inspect the repository but cannot mutate GitHub or the Project.
  Owner Control alone applies the structured result.
  """

  alias SymphonyElixir.Codex.AppServer
  alias SymphonyElixir.Tracker.Issue
  alias SymphonyElixir.Workspace

  @model "gpt-6-astra"
  @timeout_ms 600_000
  @output_schema %{
    "type" => "object",
    "additionalProperties" => false,
    "required" => ["outcome", "decision", "evidence", "assumptions", "next_step", "question"],
    "properties" => %{
      "outcome" => %{"type" => "string", "enum" => ["resolved", "unresolved"]},
      "decision" => %{"type" => ["string", "null"], "maxLength" => 2_000},
      "evidence" => %{
        "type" => "array",
        "maxItems" => 8,
        "items" => %{"type" => "string", "maxLength" => 500}
      },
      "assumptions" => %{
        "type" => "array",
        "maxItems" => 8,
        "items" => %{"type" => "string", "maxLength" => 500}
      },
      "next_step" => %{"type" => ["string", "null"], "maxLength" => 1_000},
      "question" => %{"type" => ["string", "null"], "maxLength" => 1_000}
    }
  }

  @spec run(map(), pid()) :: {:ok, map()} | {:error, term()}
  def run(context, recipient) when is_map(context) and is_pid(recipient) do
    number = positive_number(context)
    version = to_string(Map.get(context, :blocker_version) || Map.get(context, "blocker_version") || "")
    issue = review_issue(number, version, context)
    messages_key = {__MODULE__, make_ref()}
    Process.put(messages_key, [])

    on_message = fn message ->
      Process.put(messages_key, [message | Process.get(messages_key, [])])
      send(recipient, {:codex_worker_update, issue.id, message})
    end

    try do
      with {:ok, workspace} <- Workspace.create_for_issue(issue),
           :ok <- Workspace.run_before_run_hook(workspace, issue),
           {:ok, _turn} <-
             AppServer.run(workspace, prompt(context), issue,
               model: @model,
               dynamic_tools: false,
               approval_policy: "never",
               thread_sandbox: "read-only",
               turn_sandbox_policy: %{"type" => "readOnly", "networkAccess" => false},
               output_schema: @output_schema,
               turn_timeout_ms: @timeout_ms,
               runtime_account_reads: true,
               on_message: on_message
             ),
           output when is_binary(output) <- messages_key |> Process.get([]) |> agent_output(),
           {:ok, result} <- parse_output(output) do
        {:ok, result}
      else
        nil -> {:error, :missing_blocked_review_output}
        {:error, _reason} = error -> error
        error -> {:error, {:blocked_review_failed, error}}
      end
    after
      Process.delete(messages_key)
      cleanup(issue)
    end
  end

  @doc false
  @spec prompt_for_test(map()) :: String.t()
  def prompt_for_test(context), do: prompt(context)

  @doc false
  @spec parse_output_for_test(String.t()) :: {:ok, map()} | {:error, term()}
  def parse_output_for_test(output), do: parse_output(output)

  defp prompt(context) do
    """
    Review the blocked engineering issue below as a delegated technical reviewer.
    You may inspect this repository read-only. Do not edit files, run mutating commands,
    access secrets, use the network, change GitHub, or change Project status.

    Return resolved only when the blocker is a reversible local product or engineering
    decision that the normal worker can implement safely from the supplied evidence.
    Return unresolved for owner-only product choices, missing access or secrets, costs,
    production actions, security-sensitive actions, or irreversible decisions. Never
    invent credentials, requirements, or evidence. Give a concrete owner question for
    unresolved. A resolved result must give a concrete decision and next step.

    Return exactly one JSON object matching the supplied schema.

    BLOCKED_CONTEXT_JSON:
    #{Jason.encode!(context)}
    """
  end

  defp parse_output(output) when is_binary(output) do
    with [json] <- Regex.run(~r/\{.*\}/s, output),
         {:ok, decoded} <- Jason.decode(json),
         %{
           "outcome" => outcome,
           "decision" => decision,
           "evidence" => evidence,
           "assumptions" => assumptions,
           "next_step" => next_step,
           "question" => question
         } <- decoded,
         true <- outcome in ["resolved", "unresolved"],
         true <- is_list(evidence) and Enum.all?(evidence, &is_binary/1),
         true <- is_list(assumptions) and Enum.all?(assumptions, &is_binary/1),
         true <- is_nil(decision) or is_binary(decision),
         true <- is_nil(next_step) or is_binary(next_step),
         true <- is_nil(question) or is_binary(question),
         true <- outcome == "unresolved" or present?(decision),
         true <- outcome == "resolved" or present?(question) do
      {:ok, decoded}
    else
      error -> {:error, {:invalid_blocked_review_output, error}}
    end
  end

  defp agent_output(messages) when is_list(messages) do
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
  defp agent_texts(_value), do: []

  defp review_issue(number, version, context) do
    short_version = String.slice(version, 0, 12)

    %Issue{
      id: Integer.to_string(number),
      identifier: "BLOCKED-#{number}-#{short_version}",
      title: to_string(Map.get(context, :title) || Map.get(context, "title") || "Blocked review"),
      description: to_string(Map.get(context, :body) || Map.get(context, "body") || ""),
      state: "open",
      labels: [],
      dispatchable: false
    }
  end

  defp cleanup(issue) do
    Workspace.remove_issue_workspaces(issue)
    :ok
  rescue
    _error -> :ok
  end

  defp positive_number(context) do
    case Map.get(context, :number) || Map.get(context, "number") do
      number when is_integer(number) and number > 0 -> number
      value when is_binary(value) -> String.to_integer(value)
    end
  end

  defp present?(value) when is_binary(value), do: String.trim(value) != ""
  defp present?(_value), do: false
end
