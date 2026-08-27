defmodule SymphonyElixir.UsageCost do
  @moduledoc """
  Deterministic GPT-5.6 rate-card estimate for owner-facing usage.

  Values are micro-credits at the published per-million-token rates. Cached
  reads use the published 90% discount and cache writes use 1.25x input.
  Reasoning tokens are displayed separately but are already part of output.
  """

  @rates %{
    "gpt-5.6-luna" => %{input: 0.20, cached: 0.02, output: 1.20},
    "gpt-5.6-terra" => %{input: 2.00, cached: 0.20, output: 12.00},
    "gpt-5.6-sol" => %{input: 4.00, cached: 0.40, output: 20.00}
  }

  @spec estimate_micros(String.t() | nil, map()) :: non_neg_integer() | nil
  def estimate_micros(model, usage) when is_binary(model) and is_map(usage) do
    case Map.get(@rates, model) do
      nil ->
        nil

      rates ->
        input = token_value(usage, :input_tokens)
        cached = min(input, token_value(usage, :cached_input_tokens))
        cache_write = min(max(input - cached, 0), token_value(usage, :cache_write_input_tokens))
        uncached = max(input - cached - cache_write, 0)
        output = token_value(usage, :output_tokens)

        round(
          uncached * rates.input +
            cached * rates.cached +
            cache_write * rates.input * 1.25 +
            output * rates.output
        )
    end
  end

  def estimate_micros(_model, _usage), do: nil

  @doc "Approximately allocates observed account weekly movement by recorded task credits."
  @spec approximate_week_impact([map()], number() | nil) :: %{optional(String.t()) => float()}
  def approximate_week_impact(entries, used_percent)
      when is_list(entries) and is_number(used_percent) and used_percent >= 0 do
    credits_by_issue =
      Enum.reduce(entries, %{}, fn entry, totals ->
        issue_id = Map.get(entry, :issue_id)
        credits = Map.get(entry, :estimated_usage_credits_micros, 0)

        if is_binary(issue_id) and is_integer(credits) and credits > 0 do
          Map.update(totals, issue_id, credits, &(&1 + credits))
        else
          totals
        end
      end)

    total_credits = Enum.sum(Map.values(credits_by_issue))

    if total_credits > 0 do
      Map.new(credits_by_issue, fn {issue_id, credits} ->
        {issue_id, Float.round(used_percent * credits / total_credits, 2)}
      end)
    else
      %{}
    end
  end

  def approximate_week_impact(_entries, _used_percent), do: %{}

  defp token_value(usage, key) do
    case Map.get(usage, key) || Map.get(usage, Atom.to_string(key)) do
      value when is_integer(value) and value >= 0 -> value
      _ -> 0
    end
  end
end
