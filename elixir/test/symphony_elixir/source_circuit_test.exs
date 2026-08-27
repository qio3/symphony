defmodule SymphonyElixir.SourceCircuitTest do
  use ExUnit.Case, async: true

  alias SymphonyElixir.{Orchestrator, SourceCircuit}

  test "opens after three consecutive source failures and closes on success" do
    assert SourceCircuit.snapshot(SourceCircuit.new(), 0).retry_in_ms == 0
    circuit = SourceCircuit.new(threshold: 3, cooldown_ms: 60_000)

    circuit = SourceCircuit.failure(circuit, :timeout, 1_000)
    refute SourceCircuit.open?(circuit, 1_000)
    circuit = SourceCircuit.failure(circuit, :timeout, 2_000)
    refute SourceCircuit.open?(circuit, 2_000)
    circuit = SourceCircuit.failure(circuit, :forbidden, 3_000)

    assert SourceCircuit.open?(circuit, 3_000)
    assert SourceCircuit.snapshot(circuit, 3_000).failure_count == 3
    assert SourceCircuit.snapshot(circuit, 3_000).retry_in_ms == 60_000

    refute SourceCircuit.open?(circuit, 63_000)
    assert SourceCircuit.success(circuit) == SourceCircuit.new(threshold: 3, cooldown_ms: 60_000)
  end

  test "an open circuit does not count deferred work as another failure" do
    circuit = SourceCircuit.new(threshold: 1, cooldown_ms: 10_000)
    circuit = SourceCircuit.failure(circuit, :rate_limited, 500)

    assert SourceCircuit.open?(circuit, 1_000)
    assert SourceCircuit.snapshot(circuit, 1_000).failure_count == 1
  end

  test "an open shared-source circuit gates the whole polling pass" do
    now_ms = System.monotonic_time(:millisecond)

    circuit =
      SourceCircuit.new(threshold: 1, cooldown_ms: 60_000)
      |> SourceCircuit.failure(:rate_limited, now_ms)

    state = %Orchestrator.State{source_circuit: circuit}

    assert Orchestrator.maybe_dispatch_for_test(state) == state
  end
end
