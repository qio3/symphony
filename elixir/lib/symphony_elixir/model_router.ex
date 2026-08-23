defmodule SymphonyElixir.ModelRouter do
  @moduledoc """
  Selects a Codex model from issue metadata and performs bounded escalation.
  """

  alias SymphonyElixir.Codex.MetadataClassifier
  alias SymphonyElixir.Config
  alias SymphonyElixir.Tracker.Issue

  @tiers [:luna, :terra, :sol]
  @override_labels %{"model:luna" => :luna, "model:terra" => :terra, "model:sol" => :sol}

  @type tier :: :luna | :terra | :sol
  @type route :: %{
          selected_tier: tier(),
          actual_model: String.t(),
          routing_reason: String.t(),
          confidence: float(),
          escalated_from: tier() | nil,
          escalation_history: [map()],
          models: map()
        }

  @spec route(Issue.t(), keyword()) :: route()
  def route(%Issue{} = issue, opts \\ []) do
    config =
      case Keyword.fetch(opts, :config) do
        {:ok, configured} -> configured
        :error -> Config.settings!().model_routing
      end

    labels = normalized_labels(issue.labels)

    case explicit_override(labels) do
      {:ok, tier, label} ->
        build_route(tier, config, "label_override:#{label}", 1.0)

      :none ->
        case force_sol_label(labels, Map.get(config, :force_sol_labels, [])) do
          {:ok, label} ->
            build_route(:sol, config, "force_sol_label:#{label}", 1.0)

          :none ->
            classify(issue, config, Keyword.get(opts, :classifier))
        end
    end
  end

  @spec metadata(Issue.t()) :: map()
  def metadata(%Issue{} = issue) do
    %{
      title: issue.title || "",
      body: issue.description || "",
      labels: issue.labels || [],
      acceptance_criteria: extract_acceptance_criteria(issue.description),
      metadata: %{
        identifier: issue.identifier,
        priority: issue.priority,
        state: issue.state
      }
    }
  end

  @spec escalate(route(), String.t() | atom()) :: route()
  def escalate(%{selected_tier: :sol} = route, _reason), do: route

  def escalate(%{selected_tier: tier} = route, reason) when tier in [:luna, :terra] do
    next_tier = if tier == :luna, do: :terra, else: :sol
    reason = machine_reason(reason)

    %{
      route
      | selected_tier: next_tier,
        actual_model: model_id(route, next_tier),
        routing_reason: "escalation:#{reason}",
        escalated_from: tier,
        escalation_history:
          Map.get(route, :escalation_history, []) ++
            [%{from: tier, to: next_tier, reason: reason}]
    }
  end

  @spec maybe_escalate(route(), term()) :: route()
  def maybe_escalate(route, reason)
      when reason in [:max_turns_exhausted, :session_budget_exceeded, "max_turns_exhausted", "session_budget_exceeded"] do
    escalate(route, reason)
  end

  def maybe_escalate(route, _reason), do: route

  @spec retry_route(route(), term()) :: route()
  def retry_route(route, reason) do
    if non_escalating_retry?(reason) do
      route
    else
      fingerprint = failure_fingerprint(reason)
      previous = Map.get(route, :failure_fingerprint)
      count = if previous == fingerprint, do: Map.get(route, :same_failure_count, 0) + 1, else: 1

      tracked =
        route
        |> Map.put(:failure_fingerprint, fingerprint)
        |> Map.put(:same_failure_count, count)

      if count >= 2 do
        route
        |> escalate("repeated_root_cause_#{fingerprint}")
        |> Map.put(:failure_fingerprint, nil)
        |> Map.put(:same_failure_count, 0)
      else
        tracked
      end
    end
  end

  @spec exhaustion_reason(term()) :: :max_turns_exhausted | :session_budget_exceeded | nil
  def exhaustion_reason({:max_turns_exhausted, _turns}), do: :max_turns_exhausted

  def exhaustion_reason(reason) do
    if contains_error_code?(reason, "sessionBudgetExceeded"),
      do: :session_budget_exceeded,
      else: nil
  end

  defp classify(issue, config, nil) do
    classifier = &MetadataClassifier.classify/1
    classify(issue, config, classifier)
  end

  defp classify(issue, config, classifier) when is_function(classifier, 1) do
    result = classifier.(metadata(issue))
    threshold = Map.get(config, :confidence_threshold, 0.65)

    with {:ok, tier} <- result_tier(result),
         confidence when is_number(confidence) <- result_value(result, :confidence),
         true <- confidence >= threshold do
      reason = result |> result_value(:reason) |> machine_reason()
      build_route(tier, config, "classifier:#{reason}", confidence / 1.0)
    else
      false -> build_route(:terra, config, "classifier_low_confidence", 0.0)
      _ -> build_route(:terra, config, "classifier_invalid_result", 0.0)
    end
  rescue
    _ -> build_route(:terra, config, "classifier_unavailable", 0.0)
  catch
    _, _ -> build_route(:terra, config, "classifier_unavailable", 0.0)
  end

  defp build_route(tier, config, reason, confidence) do
    %{
      selected_tier: tier,
      actual_model: model_id(config, tier),
      routing_reason: reason,
      confidence: confidence,
      escalated_from: nil,
      escalation_history: [],
      models: Map.get(config, :models, default_models())
    }
  end

  defp model_id(%{actual_model: _} = route, tier) do
    route
    |> Map.get(:models, default_models())
    |> Map.get(to_string(tier), Map.fetch!(default_models(), to_string(tier)))
  end

  defp model_id(config, tier) do
    config
    |> Map.get(:models, default_models())
    |> Map.get(to_string(tier), Map.fetch!(default_models(), to_string(tier)))
  end

  defp default_models do
    %{
      "luna" => "gpt-5.6-luna",
      "terra" => "gpt-5.6-terra",
      "sol" => "gpt-5.6-sol"
    }
  end

  defp explicit_override(labels) do
    Enum.find_value(labels, :none, fn label ->
      case Map.fetch(@override_labels, label) do
        {:ok, tier} -> {:ok, tier, label}
        :error -> nil
      end
    end)
  end

  defp force_sol_label(labels, configured_labels) do
    configured = MapSet.new(normalized_labels(configured_labels))

    case Enum.find(labels, &MapSet.member?(configured, &1)) do
      nil -> :none
      label -> {:ok, label}
    end
  end

  defp normalized_labels(labels) when is_list(labels) do
    labels
    |> Enum.filter(&is_binary/1)
    |> Enum.map(&(String.trim(&1) |> String.downcase()))
    |> Enum.reject(&(&1 == ""))
  end

  defp normalized_labels(_labels), do: []

  defp result_tier(result) do
    case result_value(result, :tier) do
      tier when tier in @tiers ->
        {:ok, tier}

      tier when is_binary(tier) ->
        case String.downcase(String.trim(tier)) do
          "luna" -> {:ok, :luna}
          "terra" -> {:ok, :terra}
          "sol" -> {:ok, :sol}
          _ -> :error
        end

      _ ->
        :error
    end
  end

  defp result_value(result, key) when is_map(result) do
    Map.get(result, key) || Map.get(result, to_string(key))
  end

  defp result_value(_result, _key), do: nil

  defp extract_acceptance_criteria(description) when is_binary(description) do
    case Regex.run(
           ~r/(?:^|\n)\#{1,3}\s*(?:acceptance criteria|criteria|ac|критерии при[её]мки)\s*\n(?<criteria>.*)$/isu,
           description,
           capture: :all_names
         ) do
      [criteria] -> String.trim(criteria)
      _ -> nil
    end
  end

  defp extract_acceptance_criteria(_description), do: nil

  defp machine_reason(reason) when is_atom(reason), do: reason |> Atom.to_string() |> machine_reason()

  defp machine_reason(reason) when is_binary(reason) do
    reason
    |> String.trim()
    |> String.downcase()
    |> String.replace(~r/[^a-z0-9._-]+/u, "_")
    |> String.trim("_")
    |> String.slice(0, 80)
    |> case do
      "" -> "unspecified"
      normalized -> normalized
    end
  end

  defp machine_reason(_reason), do: "unspecified"

  defp non_escalating_retry?(reason) do
    text = reason |> inspect(limit: 50, printable_limit: 2_000) |> String.downcase()

    Enum.any?(
      [
        "ci_retry",
        "ci failure",
        "network",
        "connection",
        "econn",
        "timeout",
        "rate_limit",
        "rate limit",
        "429",
        "502",
        "503",
        "504",
        "blocked",
        "owner"
      ],
      &String.contains?(text, &1)
    )
  end

  defp failure_fingerprint(reason) do
    :sha256
    |> :crypto.hash(inspect(reason, limit: 50, printable_limit: 2_000))
    |> Base.encode16(case: :lower)
    |> String.slice(0, 12)
  end

  defp contains_error_code?(%{} = map, code) do
    Enum.any?(map, fn
      {key, ^code} when key in ["code", :code] -> true
      {_key, value} -> contains_error_code?(value, code)
    end)
  end

  defp contains_error_code?(list, code) when is_list(list),
    do: Enum.any?(list, &contains_error_code?(&1, code))

  defp contains_error_code?(tuple, code) when is_tuple(tuple),
    do: tuple |> Tuple.to_list() |> contains_error_code?(code)

  defp contains_error_code?(_value, _code), do: false
end
