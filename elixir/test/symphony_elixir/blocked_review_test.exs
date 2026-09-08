defmodule SymphonyElixir.BlockedReviewTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.BlockedReview

  test "review runs through GH workspace hooks without touching the delivery workspace" do
    root = Path.join(System.tmp_dir!(), "blocked-review-hooks-#{System.unique_integer([:positive])}")
    workspace_root = Path.join(root, "workspaces")
    delivery = Path.join(workspace_root, "GH-892")
    trace = Path.join(root, "requests.jsonl")
    fake_codex = Path.join(root, "fake-codex")
    arguments = Path.join(root, "arguments.txt")
    File.mkdir_p!(delivery)
    File.write!(Path.join(delivery, "user-work.txt"), "preserve")
    on_exit(fn -> File.rm_rf!(root) end)

    result = %{
      "outcome" => "resolved",
      "decision" => "Reuse the existing control.",
      "evidence" => ["Fixture supplies the existing control."],
      "assumptions" => [],
      "next_step" => "Implement the existing pattern.",
      "question" => nil
    }

    notification = Jason.encode!(%{method: "item/completed", params: %{item: %{type: "agentMessage", text: Jason.encode!(result)}}})

    File.write!(fake_codex, """
    #!/bin/sh
    printf '%s\\n' "$*" > '#{arguments}'
    while IFS= read -r line; do
      printf '%s\\n' "$line" >> '#{trace}'
      case "$line" in
        *'"method":"initialize"'*) printf '%s\\n' '{"id":1,"result":{}}' ;;
        *'"method":"account/read"'*) printf '%s\\n' '{"id":4,"result":{"account":{"planType":"pro"}}}' ;;
        *'"method":"account/rateLimits/read"'*) printf '%s\\n' '{"id":5,"result":{"rateLimits":{}}}' ;;
        *'"method":"thread/start"'*) printf '%s\\n' '{"id":2,"result":{"thread":{"id":"review-thread"}}}' ;;
        *'"method":"turn/start"'*)
          printf '%s\\n' '{"id":3,"result":{"turn":{"id":"review-turn"}}}'
          printf '%s\\n' '#{notification}' '{"method":"turn/completed"}' ;;
        *'"method":"account/usage/read"'*) printf '%s\\n' '{"id":6,"result":{}}' ;;
      esac
    done
    """)

    File.chmod!(fake_codex, 0o755)

    write_workflow_file!(Workflow.workflow_file_path(),
      workspace_root: workspace_root,
      codex_command: fake_codex,
      hook_after_create: """
      set -eu
      identifier="${PWD##*/}"
      issue_number="${identifier#GH-}"
      test "$issue_number" != "$identifier"
      git init -q
      git switch -c "codex/issue-${issue_number}-symphony"
      """,
      hook_before_run: """
      set -eu
      test -d .git
      case "$(git branch --show-current)" in
        codex/issue-*-symphony) ;;
        *) exit 2 ;;
      esac
      """
    )

    assert {:ok, ^result} = BlockedReview.run(%{number: 892, blocker_version: "fixture-v1"}, self())
    assert File.read!(Path.join(delivery, "user-work.txt")) == "preserve"
    assert File.ls!(workspace_root) == ["GH-892"]
    if :os.type() == {:unix, :linux}, do: assert(File.read!(arguments) =~ "--enable use_legacy_landlock")

    requests = trace |> File.read!() |> String.split("\n", trim: true) |> Enum.map(&Jason.decode!/1)
    thread = Enum.find(requests, &(&1["method"] == "thread/start"))["params"]
    turn = Enum.find(requests, &(&1["method"] == "turn/start"))["params"]
    assert thread["model"] == "gpt-6-astra"
    assert thread["sandbox"] == "read-only"
    assert thread["approvalPolicy"] == "never"
    assert turn["sandboxPolicy"] == %{"type" => "readOnly", "networkAccess" => false}
    assert thread["dynamicTools"] == []
  end

  test "prompt keeps the model read-only and reserves owner-only decisions" do
    prompt = BlockedReview.prompt_for_test(%{number: 892, title: "Review blocker"})

    assert prompt =~ "read-only"
    assert prompt =~ "Do not edit files"
    assert prompt =~ "missing access or secrets"
    assert prompt =~ "production actions"
  end

  test "parser accepts a structured resolved decision" do
    output =
      Jason.encode!(%{
        outcome: "resolved",
        decision: "Use the existing adapter.",
        evidence: ["The adapter is already covered by tests."],
        assumptions: [],
        next_step: "Return the issue to Ready for AI.",
        question: nil
      })

    assert {:ok, %{"outcome" => "resolved"}} = BlockedReview.parse_output_for_test(output)
  end

  test "parser rejects unresolved output without a concrete owner question" do
    output =
      Jason.encode!(%{
        outcome: "unresolved",
        decision: nil,
        evidence: [],
        assumptions: [],
        next_step: nil,
        question: nil
      })

    assert {:error, {:invalid_blocked_review_output, false}} =
             BlockedReview.parse_output_for_test(output)
  end
end
