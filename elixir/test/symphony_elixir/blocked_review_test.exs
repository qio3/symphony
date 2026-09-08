defmodule SymphonyElixir.BlockedReviewTest do
  use ExUnit.Case, async: true

  alias SymphonyElixir.BlockedReview

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
