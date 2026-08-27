defmodule SymphonyElixir.SourceCircuit do
  @moduledoc """
  Small in-memory circuit breaker for the shared issue/control source.

  It prevents one upstream outage from turning every claimed issue into its
  own retry loop. Deferred work does not increment the failure counter.
  """

  @type t :: %{
          threshold: pos_integer(),
          cooldown_ms: pos_integer(),
          failure_count: non_neg_integer(),
          opened_until_ms: integer() | nil,
          last_error: term()
        }

  @spec new(keyword()) :: t()
  def new(opts \\ []) do
    %{
      threshold: Keyword.get(opts, :threshold, 3),
      cooldown_ms: Keyword.get(opts, :cooldown_ms, 60_000),
      failure_count: 0,
      opened_until_ms: nil,
      last_error: nil
    }
  end

  @spec failure(t(), term(), integer()) :: t()
  def failure(circuit, reason, now_ms) when is_map(circuit) and is_integer(now_ms) do
    failure_count = circuit.failure_count + 1

    opened_until_ms =
      if failure_count >= circuit.threshold,
        do: now_ms + circuit.cooldown_ms,
        else: nil

    %{circuit | failure_count: failure_count, opened_until_ms: opened_until_ms, last_error: reason}
  end

  @spec success(t()) :: t()
  def success(circuit) when is_map(circuit) do
    new(threshold: circuit.threshold, cooldown_ms: circuit.cooldown_ms)
  end

  @spec open?(t(), integer()) :: boolean()
  def open?(%{opened_until_ms: opened_until_ms}, now_ms)
      when is_integer(opened_until_ms) and is_integer(now_ms),
      do: now_ms < opened_until_ms

  def open?(_circuit, _now_ms), do: false

  @spec snapshot(t(), integer()) :: map()
  def snapshot(circuit, now_ms) when is_map(circuit) and is_integer(now_ms) do
    retry_in_ms =
      case circuit.opened_until_ms do
        until_ms when is_integer(until_ms) -> max(0, until_ms - now_ms)
        _ -> 0
      end

    %{
      open: open?(circuit, now_ms),
      failure_count: circuit.failure_count,
      retry_in_ms: retry_in_ms,
      last_error: circuit.last_error
    }
  end
end
