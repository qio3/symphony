defmodule SymphonyElixir.ModelRouterTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.Codex.MetadataClassifier
  alias SymphonyElixir.ModelRouter

  @config %{
    classifier_model: "gpt-5.6-luna",
    confidence_threshold: 0.65,
    models: %{
      "luna" => "gpt-5.6-luna",
      "terra" => "gpt-5.6-terra",
      "sol" => "gpt-5.6-sol"
    },
    force_sol_labels: ["risk:security-critical"]
  }

  test "WORKFLOW config owns the routing policy" do
    write_workflow_file!(Workflow.workflow_file_path(),
      model_routing_enabled: true,
      model_routing_confidence_threshold: 0.72,
      model_routing_force_sol_labels: ["risk:data-integrity"],
      model_routing_models: @config.models
    )

    routing = Config.settings!().model_routing
    assert routing.enabled
    assert routing.classifier_model == "gpt-5.6-luna"
    assert routing.classifier_command =~ "--disable shell_tool"
    assert routing.classifier_command =~ "--disable unified_exec"
    assert routing.confidence_threshold == 0.72
    assert routing.models == @config.models
    assert routing.force_sol_labels == ["risk:data-integrity"]
  end

  test "classifier prompt contains only issue metadata and parses structured output" do
    metadata = ModelRouter.metadata(issue("simple"))
    prompt = MetadataClassifier.prompt_for_test(metadata)

    assert prompt =~ Jason.encode!(metadata)
    refute prompt =~ "inspect the repository"

    assert {:ok, %{tier: "luna", confidence: 0.91, reason: "bounded_local_fix"}} =
             MetadataClassifier.parse_output_for_test(~s|```json\n{"tier":"luna","confidence":0.91,"reason":"bounded_local_fix"}\n```|)
  end

  test "explicit model label is the owner override" do
    route =
      ModelRouter.route(%{issue("small copy fix") | labels: ["model:sol"]},
        config: @config,
        classifier: fn _metadata -> flunk("classifier must not run") end
      )

    assert route.selected_tier == :sol
    assert route.actual_model == "gpt-5.6-sol"
    assert route.routing_reason == "label_override:model:sol"
  end

  test "metadata-only classifier routes simple normal and complex issues" do
    classifier = fn metadata ->
      assert Map.keys(metadata) |> Enum.sort() ==
               [:acceptance_criteria, :body, :labels, :metadata, :title]

      case metadata.title do
        "simple" -> %{tier: "luna", confidence: 0.92, reason: "bounded_local_fix"}
        "normal" -> %{tier: "terra", confidence: 0.84, reason: "multi_file_feature"}
        "complex" -> %{tier: "sol", confidence: 0.91, reason: "concurrency_root_cause"}
      end
    end

    assert ModelRouter.route(issue("simple"), config: @config, classifier: classifier).selected_tier == :luna
    assert ModelRouter.route(issue("normal"), config: @config, classifier: classifier).selected_tier == :terra
    assert ModelRouter.route(issue("complex"), config: @config, classifier: classifier).selected_tier == :sol
  end

  test "low classifier confidence falls back to Terra" do
    route =
      ModelRouter.route(issue("ambiguous"),
        config: @config,
        classifier: fn _ -> %{tier: "sol", confidence: 0.4, reason: "uncertain"} end
      )

    assert route.selected_tier == :terra
    assert route.actual_model == "gpt-5.6-terra"
    assert route.routing_reason == "classifier_low_confidence"
  end

  test "an exact configured high-risk label forces Sol without classifier" do
    route =
      ModelRouter.route(%{issue("rotate auth") | labels: ["risk:security-critical"]},
        config: @config,
        classifier: fn _ -> flunk("classifier must not run") end
      )

    assert route.selected_tier == :sol
    assert route.routing_reason == "force_sol_label:risk:security-critical"
  end

  test "reasoning exhaustion escalates one tier and Sol is the ceiling" do
    luna = ModelRouter.route(%{issue("small") | labels: ["model:luna"]}, config: @config)
    terra = ModelRouter.escalate(luna, "max_turns_exhausted")
    sol = ModelRouter.escalate(terra, "session_budget_exceeded")

    assert terra.selected_tier == :terra
    assert terra.escalated_from == :luna
    assert terra.actual_model == "gpt-5.6-terra"
    assert sol.selected_tier == :sol
    assert sol.escalated_from == :terra
    assert sol.actual_model == "gpt-5.6-sol"
    assert length(sol.escalation_history) == 2
    assert ModelRouter.escalate(sol, "max_turns_exhausted") == sol
  end

  test "CI retry and owner Blocked do not escalate" do
    luna = ModelRouter.route(%{issue("small") | labels: ["model:luna"]}, config: @config)

    assert ModelRouter.maybe_escalate(luna, :ci_retry) == luna
    assert ModelRouter.maybe_escalate(luna, :network_retry) == luna
    assert ModelRouter.maybe_escalate(luna, :owner_blocked) == luna
    assert ModelRouter.retry_route(luna, {:ci_retry, "tests failed"}) == luna
    assert ModelRouter.retry_route(luna, {:network_retry, :econnreset}) == luna
  end

  test "two identical non-transient root-cause failures escalate one tier" do
    luna = ModelRouter.route(%{issue("small") | labels: ["model:luna"]}, config: @config)
    first_retry = ModelRouter.retry_route(luna, {:worker_failed, :same_root_cause})
    second_retry = ModelRouter.retry_route(first_retry, {:worker_failed, :same_root_cause})

    assert first_retry.selected_tier == :luna
    assert second_retry.selected_tier == :terra
    assert second_retry.actual_model == "gpt-5.6-terra"
    assert second_retry.routing_reason =~ "escalation:repeated_root_cause_"
    assert List.last(second_retry.escalation_history).reason =~ "repeated_root_cause_"
  end

  test "only the exact app-server budget code is reasoning exhaustion" do
    assert ModelRouter.exhaustion_reason({:turn_failed, %{"turn" => %{"error" => %{"code" => "sessionBudgetExceeded"}}}}) == :session_budget_exceeded

    assert ModelRouter.exhaustion_reason({:turn_failed, %{"turn" => %{"error" => %{"code" => "ciFailed"}}}}) == nil
  end

  test "runtime snapshot exposes worker routing and active/completed aggregates" do
    write_workflow_file!(Workflow.workflow_file_path(), tracker_kind: "memory", poll_interval_ms: 60_000)
    orchestrator_name = Module.concat(__MODULE__, :SnapshotOrchestrator)
    {:ok, pid} = Orchestrator.start_link(name: orchestrator_name)

    on_exit(fn ->
      if Process.alive?(pid), do: Process.exit(pid, :normal)
    end)

    entry = %{
      identifier: "GH-44",
      issue: %Issue{id: "44", identifier: "GH-44", state: "In Progress"},
      session_id: "thread-44",
      codex_app_server_pid: "123",
      codex_input_tokens: 1,
      codex_output_tokens: 2,
      codex_total_tokens: 3,
      last_codex_timestamp: nil,
      last_codex_message: nil,
      last_codex_event: nil,
      started_at: DateTime.utc_now(),
      selected_model_tier: :terra,
      actual_model: "gpt-5.6-terra",
      routing_reason: "classifier:multi_file_feature",
      escalated_from: :luna,
      escalation_history: [%{from: :luna, to: :terra, reason: "max_turns_exhausted"}]
    }

    :sys.replace_state(pid, fn state ->
      %{
        state
        | running: %{"44" => entry},
          model_completed_counts: %{luna: 2, terra: 1, sol: 0}
      }
    end)

    snapshot = Orchestrator.snapshot(orchestrator_name, 1_000)
    assert snapshot.model_counts.terra == %{active: 1, completed: 1}
    assert snapshot.model_counts.luna == %{active: 0, completed: 2}

    assert [worker] = snapshot.running
    assert worker.selected_model_tier == :terra
    assert worker.actual_model == "gpt-5.6-terra"
    assert worker.routing_reason == "classifier:multi_file_feature"
    assert worker.escalated_from == :luna
    assert length(worker.escalation_history) == 1
  end

  defp issue(title) do
    %Issue{
      id: "issue-#{title}",
      identifier: "GH-1",
      title: title,
      description: "Body\n\n## Acceptance criteria\n- verified",
      priority: 2,
      state: "In Progress",
      labels: ["symphony"],
      dispatchable: true
    }
  end
end
