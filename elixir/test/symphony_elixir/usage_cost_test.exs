defmodule SymphonyElixir.UsageCostTest do
  use ExUnit.Case, async: true

  alias SymphonyElixir.UsageCost

  test "estimates current GPT-5.6 Luna Terra and Sol credits from token classes" do
    usage = %{
      input_tokens: 1_000_000,
      cached_input_tokens: 200_000,
      cache_write_input_tokens: 100_000,
      output_tokens: 100_000,
      reasoning_output_tokens: 20_000,
      total_tokens: 1_100_000
    }

    assert UsageCost.estimate_micros("gpt-5.6-luna", usage) == 289_000
    assert UsageCost.estimate_micros("gpt-5.6-terra", usage) == 2_890_000
    assert UsageCost.estimate_micros("gpt-5.6-sol", usage) == 5_380_000
  end

  test "unknown models remain unavailable instead of inventing a price" do
    assert UsageCost.estimate_micros("gpt-unknown", %{total_tokens: 10}) == nil
    assert UsageCost.estimate_micros(nil, %{total_tokens: 10}) == nil
    assert UsageCost.estimate_micros("gpt-5.6-luna", :invalid) == nil
    assert UsageCost.estimate_micros("gpt-5.6-luna", %{input_tokens: "invalid"}) == 0
  end

  test "attributes observed weekly movement proportionally and explicitly approximately" do
    entries = [
      %{issue_id: "401", estimated_usage_credits_micros: 250},
      %{issue_id: "402", estimated_usage_credits_micros: 750}
    ]

    assert UsageCost.approximate_week_impact(entries, 20) == %{
             "401" => 5.0,
             "402" => 15.0
           }

    assert UsageCost.approximate_week_impact(entries, nil) == %{}
    assert UsageCost.approximate_week_impact([%{issue_id: nil, estimated_usage_credits_micros: 50}], 20) == %{}
    assert UsageCost.approximate_week_impact([], 20) == %{}
  end
end
