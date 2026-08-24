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

  test "ignores malformed JSON and invalid persisted entries" do
    path = temporary_path()

    on_exit(fn -> File.rm(path) end)

    File.write!(
      path,
      "{not json}\n" <>
        "[]\n" <>
        ~s({"issue_id":"missing-thread","token_usage":{"total_tokens":5}}\n) <>
        ~s({"issue_id":"empty-thread","thread_id":"","token_usage":{"total_tokens":5}}\n)
    )

    assert {:ok, ledger} = UsageLedger.load(path)
    assert UsageLedger.snapshot(ledger).current == []
    assert UsageLedger.snapshot(ledger).aggregate.token_usage.total_tokens == 0
  end

  test "normalizes binary numerics, invalid datetimes, and non-map usage" do
    path = temporary_path()
    on_exit(fn -> File.rm(path) end)
    assert {:ok, ledger} = UsageLedger.load(path)

    assert {:ok, ledger} =
             UsageLedger.record(ledger, %{
               "issue_id" => "issue-binary",
               "thread_id" => "thread-binary",
               "tier" => "terra",
               "started_at" => "not-a-datetime",
               "completed_at" => "also-not-a-datetime",
               "estimated_usage_credits_micros" => "42",
               "token_usage" => %{
                 "input_tokens" => "12",
                 "cached_input_tokens" => "invalid",
                 "total_tokens" => "21"
               }
             })

    assert {:ok, ledger} =
             UsageLedger.record(ledger, %{
               issue_id: "issue-non-map-usage",
               thread_id: "thread-non-map-usage",
               token_usage: "not a map"
             })

    current = UsageLedger.snapshot(ledger).current
    binary = Enum.find(current, &(&1.issue_id == "issue-binary"))
    non_map = Enum.find(current, &(&1.issue_id == "issue-non-map-usage"))

    assert binary.tier == :terra
    assert binary.started_at == nil
    assert binary.completed_at == nil
    assert binary.estimated_usage_credits_micros == 42
    assert binary.token_usage.input_tokens == 12
    assert binary.token_usage.cached_input_tokens == 0
    assert binary.token_usage.total_tokens == 21
    assert non_map.token_usage.total_tokens == 0
  end

  test "rejects invalid records and leaves a missing completion unchanged" do
    assert {:ok, ledger} = UsageLedger.load(temporary_path())
    assert {:error, :invalid_usage_entry} = UsageLedger.record(ledger, %{})

    assert {:ok, unchanged} =
             UsageLedger.complete(ledger, "missing-issue", "missing-thread", ~U[2026-08-24 10:05:00Z])

    assert unchanged == ledger
  end

  test "returns filesystem errors from unreadable and unwritable ledger paths" do
    path = temporary_path()
    File.mkdir_p!(path)

    on_exit(fn -> File.rm_rf(path) end)

    assert {:error, _reason} = UsageLedger.load(path)

    ledger = %{path: path, entries: %{}}
    assert {:error, _reason} = UsageLedger.record(ledger, entry_for("issue-write", "thread-write", 1))
  end

  test "keeps existing high-water fields when a later sample regresses" do
    path = temporary_path()
    on_exit(fn -> File.rm(path) end)

    first = entry_for("issue-high-water", "thread-high-water", 20, input_tokens: 10, model: "first")

    regressed =
      entry_for("issue-high-water", "thread-high-water", 10,
        input_tokens: 99,
        model: "second",
        session_id: "turn-2"
      )

    advanced = entry_for("issue-high-water", "thread-high-water", 30, input_tokens: 5, model: "third")

    File.write!(path, Jason.encode!(first) <> "\n" <> Jason.encode!(regressed) <> "\n")

    assert {:ok, ledger} = UsageLedger.load(path)
    assert [after_regression] = UsageLedger.snapshot(ledger).current
    assert after_regression.token_usage.total_tokens == 20
    assert after_regression.token_usage.input_tokens == 10
    assert after_regression.model == "first"
    assert after_regression.session_id == "turn-2"

    assert {:ok, ledger} = UsageLedger.record(ledger, advanced)
    assert [after_advance] = UsageLedger.snapshot(ledger).current
    assert after_advance.token_usage.total_tokens == 30
    assert after_advance.token_usage.input_tokens == 10
    assert after_advance.model == "third"
  end

  defp entry_for(issue_id, thread_id, total_tokens, opts \\ []) do
    %{
      issue_id: issue_id,
      thread_id: thread_id,
      session_id: Keyword.get(opts, :session_id),
      model: Keyword.get(opts, :model),
      token_usage: %{
        input_tokens: Keyword.get(opts, :input_tokens, 0),
        total_tokens: total_tokens
      }
    }
  end

  defp temporary_path do
    Path.join(System.tmp_dir!(), "symphony-usage-#{System.unique_integer([:positive])}.jsonl")
  end
end
