defmodule SymphonyElixir.UsageLedgerTest do
  use ExUnit.Case, async: true

  alias SymphonyElixir.UsageLedger

  test "persists only high-water cumulative usage and restores aggregate entries" do
    path = Path.join(System.tmp_dir!(), "symphony-usage-#{System.unique_integer([:positive])}.jsonl")

    on_exit(fn -> File.rm(path) end)

    {:ok, ledger} = UsageLedger.load(path)

    entry = %{
      issue_id: "issue-42",
      issue_identifier: "GH-42",
      thread_id: "thread-42",
      session_id: "thread-42-turn-1",
      model: "gpt-5.6-luna",
      tier: :luna,
      started_at: ~U[2026-08-24 10:00:00Z],
      completed_at: nil,
      estimated_usage_credits_micros: 42_000,
      estimated_usage_groups: ["subscription"],
      token_usage: %{
        input_tokens: 10,
        cached_input_tokens: 2,
        cache_write_input_tokens: 1,
        output_tokens: 5,
        reasoning_output_tokens: 3,
        total_tokens: 15
      }
    }

    {:ok, ledger} = UsageLedger.record(ledger, entry)
    {:ok, ledger} = UsageLedger.record(ledger, put_in(entry, [:token_usage, :total_tokens], 12))
    {:ok, ledger} = UsageLedger.record(ledger, put_in(entry, [:token_usage, :total_tokens], 21))

    assert UsageLedger.snapshot(ledger).aggregate.token_usage.total_tokens == 21
    assert UsageLedger.snapshot(ledger).aggregate.token_usage.cached_input_tokens == 2
    assert [current] = UsageLedger.snapshot(ledger).current
    assert current.issue_id == "issue-42"
    assert current.issue_identifier == "GH-42"
    assert current.thread_id == "thread-42"
    assert current.token_usage.reasoning_output_tokens == 3
    assert current.estimated_usage_credits_micros == 42_000
    assert current.estimated_usage_groups == ["subscription"]
    assert UsageLedger.snapshot(ledger).aggregate.estimated_usage_credits_micros == 42_000

    {:ok, restored} = UsageLedger.load(path)
    assert UsageLedger.snapshot(restored) == UsageLedger.snapshot(ledger)
    assert File.read!(path) |> String.split("\n", trim: true) |> length() == 2

    {:ok, completed} = UsageLedger.complete(ledger, "issue-42", "thread-42", ~U[2026-08-24 10:05:00Z])
    assert [completed_entry] = UsageLedger.snapshot(completed).current
    assert completed_entry.completed_at == ~U[2026-08-24 10:05:00Z]
  end
end
