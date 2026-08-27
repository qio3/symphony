defmodule SymphonyElixir.UsageLedger do
  @moduledoc """
  Append-only, host-local accounting for cumulative Codex thread usage.

  The ledger keeps the latest high-water sample for every issue/thread pair in
  memory and reconstructs that view from JSONL after an orchestrator restart.
  """

  @token_fields [
    :input_tokens,
    :cached_input_tokens,
    :cache_write_input_tokens,
    :output_tokens,
    :reasoning_output_tokens,
    :total_tokens
  ]

  alias SymphonyElixir.UsageCost

  @type token_usage :: %{required(atom()) => non_neg_integer()}
  @type entry :: %{required(:issue_id) => String.t(), required(:thread_id) => String.t()}
  @type t :: %{path: Path.t(), entries: %{optional({String.t(), String.t()}) => map()}}

  @spec load(Path.t()) :: {:ok, t()} | {:error, term()}
  def load(path) when is_binary(path) do
    with :ok <- File.mkdir_p(Path.dirname(path)),
         {:ok, contents} <- read_or_empty(path) do
      entries = load_entries(contents)

      {:ok, %{path: path, entries: entries}}
    end
  end

  @spec record(t(), map()) :: {:ok, t()} | {:error, term()}
  def record(ledger, entry) when is_map(entry) do
    with {:ok, normalized} <- normalize_entry(entry) do
      maybe_append(ledger, normalized)
    end
  end

  @spec complete(t(), String.t(), String.t(), DateTime.t()) :: {:ok, t()} | {:error, term()}
  def complete(%{entries: entries} = ledger, issue_id, thread_id, %DateTime{} = completed_at)
      when is_binary(issue_id) and is_binary(thread_id) do
    case Map.get(entries, {issue_id, thread_id}) do
      nil -> {:ok, ledger}
      entry -> record(ledger, %{entry | completed_at: completed_at})
    end
  end

  @spec snapshot(t()) :: %{aggregate: map(), current: [map()]}
  def snapshot(%{entries: entries}) do
    current = entries |> Map.values() |> Enum.sort_by(&{&1.issue_id, &1.thread_id})

    aggregate =
      Enum.reduce(current, empty_usage(), fn entry, totals ->
        add_usage(totals, entry.token_usage)
      end)

    credits = Enum.reduce(current, 0, &(&1.estimated_usage_credits_micros + &2))
    %{aggregate: %{token_usage: aggregate, estimated_usage_credits_micros: credits}, current: current}
  end

  defp read_or_empty(path) do
    case File.read(path) do
      {:ok, contents} -> {:ok, contents}
      {:error, :enoent} -> {:ok, ""}
      {:error, reason} -> {:error, reason}
    end
  end

  defp load_entries(contents) do
    contents
    |> String.split("\n", trim: true)
    |> Enum.reduce(%{}, &merge_json_line/2)
  end

  defp merge_json_line(line, entries) do
    with {:ok, decoded} <- Jason.decode(line),
         {:ok, entry} <- normalize_entry(decoded) do
      merge_entry(entries, entry)
    else
      _ -> entries
    end
  end

  defp normalize_entry(entry) when is_map(entry) do
    issue_id = value(entry, :issue_id)
    thread_id = value(entry, :thread_id)

    if is_binary(issue_id) and issue_id != "" and is_binary(thread_id) and thread_id != "" do
      model = value(entry, :model)
      token_usage = normalize_usage(value(entry, :token_usage))
      reported_credits = non_negative(value(entry, :estimated_usage_credits_micros))
      estimated_credits = UsageCost.estimate_micros(model, token_usage) || 0

      {:ok,
       %{
         issue_id: issue_id,
         issue_identifier: value(entry, :issue_identifier),
         thread_id: thread_id,
         session_id: value(entry, :session_id),
         model: model,
         tier: normalize_tier(value(entry, :tier)),
         started_at: normalize_datetime(value(entry, :started_at)),
         completed_at: normalize_datetime(value(entry, :completed_at)),
         estimated_usage_credits_micros: if(reported_credits > 0, do: reported_credits, else: estimated_credits),
         estimated_usage_groups: value(entry, :estimated_usage_groups),
         token_usage: token_usage
       }}
    else
      {:error, :invalid_usage_entry}
    end
  end

  defp normalize_entry(_entry), do: {:error, :invalid_usage_entry}

  defp merge_entry(entries, entry) do
    key = entry_key(entry)
    Map.update(entries, key, entry, &merge_samples(&1, entry))
  end

  defp merge_samples(existing, incoming) do
    if incoming.token_usage.total_tokens >= existing.token_usage.total_tokens do
      Map.merge(existing, incoming, &merge_newer_value/3)
    else
      Map.merge(incoming, existing, &prefer_existing_value/3)
    end
  end

  defp maybe_append(%{entries: entries} = ledger, normalized) do
    if should_append?(Map.get(entries, entry_key(normalized)), normalized) do
      append_entry(ledger, normalized)
    else
      {:ok, ledger}
    end
  end

  defp append_entry(%{path: path, entries: entries} = ledger, normalized) do
    line = Jason.encode!(json_entry(normalized)) <> "\n"

    case File.write(path, line, [:append, :binary]) do
      :ok -> {:ok, %{ledger | entries: merge_entry(entries, normalized)}}
      {:error, reason} -> {:error, reason}
    end
  end

  defp merge_newer_value(:token_usage, old, new), do: Map.merge(old, new, &max_usage_value/3)
  defp merge_newer_value(_key, old, new), do: if(is_nil(new), do: old, else: new)
  defp prefer_existing_value(_key, new, old), do: if(is_nil(old), do: new, else: old)
  defp max_usage_value(_field, old, new), do: max(old, new)

  defp should_append?(nil, _entry), do: true

  defp should_append?(existing, incoming) do
    incoming.token_usage.total_tokens > existing.token_usage.total_tokens or
      (is_nil(existing.completed_at) and not is_nil(incoming.completed_at)) or
      existing.estimated_usage_credits_micros != incoming.estimated_usage_credits_micros or
      existing.estimated_usage_groups != incoming.estimated_usage_groups
  end

  defp entry_key(entry), do: {entry.issue_id, entry.thread_id}

  defp normalize_usage(usage) when is_map(usage) do
    Enum.into(@token_fields, %{}, fn field -> {field, non_negative(value(usage, field))} end)
  end

  defp normalize_usage(_usage), do: empty_usage()

  defp empty_usage, do: Map.new(@token_fields, &{&1, 0})

  defp add_usage(left, right) do
    Enum.into(@token_fields, %{}, fn field -> {field, Map.fetch!(left, field) + Map.fetch!(right, field)} end)
  end

  defp json_entry(entry) do
    entry
    |> Map.update!(:tier, &if(&1, do: Atom.to_string(&1), else: nil))
    |> Map.update!(:started_at, &format_datetime/1)
    |> Map.update!(:completed_at, &format_datetime/1)
    |> Map.update!(:token_usage, fn usage ->
      Map.new(usage, fn {key, value} -> {Atom.to_string(key), value} end)
    end)
    |> Map.new(fn {key, value} -> {Atom.to_string(key), value} end)
  end

  defp value(map, key), do: Map.get(map, key) || Map.get(map, Atom.to_string(key))

  defp non_negative(value) when is_integer(value) and value >= 0, do: value

  defp non_negative(value) when is_binary(value) do
    case Integer.parse(value) do
      {integer, ""} when integer >= 0 -> integer
      _ -> 0
    end
  end

  defp non_negative(_value), do: 0

  defp normalize_tier(tier) when tier in [:luna, :terra, :sol], do: tier
  defp normalize_tier(tier) when tier in ["luna", "terra", "sol"], do: String.to_existing_atom(tier)
  defp normalize_tier(_tier), do: nil

  defp normalize_datetime(%DateTime{} = value), do: value

  defp normalize_datetime(value) when is_binary(value) do
    case DateTime.from_iso8601(value) do
      {:ok, datetime, _offset} -> datetime
      _ -> nil
    end
  end

  defp normalize_datetime(_value), do: nil

  defp format_datetime(nil), do: nil
  defp format_datetime(%DateTime{} = value), do: DateTime.to_iso8601(value)
end
