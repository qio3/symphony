defmodule SymphonyElixir.OwnerControlTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.Orchestrator
  alias SymphonyElixir.OwnerControl.Client
  alias SymphonyElixir.Tracker.Issue

  defmodule PausedControl do
    def intake_active?, do: false
  end

  defmodule ActiveControl do
    def intake_active?, do: true
  end

  setup do
    keys = [
      :owner_control_settings,
      :owner_control_request_fun,
      :owner_control_client_module
    ]

    previous = Map.new(keys, &{&1, Application.get_env(:symphony_elixir, &1)})

    on_exit(fn ->
      Enum.each(previous, fn {key, value} ->
        if is_nil(value),
          do: Application.delete_env(:symphony_elixir, key),
          else: Application.put_env(:symphony_elixir, key, value)
      end)
    end)

    :ok
  end

  test "client reads the authenticated snapshot and only allows fixed typed actions" do
    configure_client()
    test_pid = self()

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn method, url, headers, body ->
      send(test_pid, {:request, method, url, headers, body})

      payload =
        cond do
          String.ends_with?(url, "/v1/intake") ->
            %{"active" => true}

          method == :get ->
            %{"version" => 1, "intake" => %{"active" => true}, "counts" => %{"running" => 1}}

          true ->
            %{"status" => "accepted"}
        end

      {:ok, 200, Jason.encode!(payload)}
    end)

    assert {:ok, %{version: 1, intake: %{active: true}, counts: %{running: 1}}} = Client.snapshot()
    assert_receive {:request, :get, "http://127.0.0.1:4080/v1/snapshot", headers, nil}
    assert {"authorization", "Bearer " <> _token} = List.keyfind(headers, "authorization", 0)

    assert {:ok, %{status: "accepted"}} = Client.action(:run, %{issue: 401})
    assert_receive {:request, :post, "http://127.0.0.1:4080/v1/actions/run", _headers, %{issue: 401}}

    assert {:ok, %{status: "accepted"}} = Client.action(:start_service, %{})
    assert_receive {:request, :post, "http://127.0.0.1:4080/v1/actions/start_service", _headers, %{}}

    assert {:ok, %{status: "accepted"}} =
             Client.action(:stop_service, %{confirm_running_workers: 1})

    assert_receive {:request, :post, "http://127.0.0.1:4080/v1/actions/stop_service", _headers, %{confirm_running_workers: 1}}

    assert {:error, :unsupported_action} = Client.action(:shell, %{command: "whoami"})

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      {:ok, 409, Jason.encode!(%{error: %{code: "action_rejected", message: "TEST has drift"}})}
    end)

    assert {:error, {:owner_control_action_rejected, "TEST has drift"}} =
             Client.action(:accept, %{issue: 402})

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn method, url, headers, body ->
      send(test_pid, {:request, method, url, headers, body})
      {:ok, 200, Jason.encode!(%{active: true})}
    end)

    assert Client.intake_active?()
    assert_receive {:request, :get, "http://127.0.0.1:4080/v1/intake", _headers, nil}
  end

  test "intake is open when control is disabled and fail-closed when configured control is unavailable" do
    Application.delete_env(:symphony_elixir, :owner_control_settings)
    assert Client.intake_active?()

    configure_client()

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      {:error, :econnrefused}
    end)

    refute Client.intake_active?()
  end

  test "client normalizes quota, usage, and model fields from shared JSON" do
    configure_client()

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      {:ok, 200,
       Jason.encode!(%{
         "version" => 1,
         "quota" => %{
           "weekly" => %{"used_percent" => 42, "window_duration_mins" => 10_080}
         },
         "issue_usage" => %{
           "401" => %{
             "total_tokens" => 1_500,
             "estimated_credits_micros" => 44_000
           }
         },
         "models" => %{"terra" => %{"active" => 1, "completed" => 2}},
         "owner_view" => %{
           "work_items" => [
             %{
               "number" => 401,
               "model" => %{
                 "selected_tier" => "terra",
                 "actual_model" => "gpt-5.6-terra",
                 "routing_reason" => "escalation:max_turns_exhausted",
                 "escalated_from" => "luna"
               }
             }
           ]
         }
       })}
    end)

    assert {:ok,
            %{
              quota: %{weekly: %{used_percent: 42, window_duration_mins: 10_080}},
              issue_usage: %{
                "401" => %{total_tokens: 1_500, estimated_credits_micros: 44_000}
              },
              models: %{terra: %{active: 1, completed: 2}},
              owner_view: %{
                work_items: [
                  %{
                    model: %{
                      selected_tier: "terra",
                      actual_model: "gpt-5.6-terra",
                      routing_reason: "escalation:max_turns_exhausted",
                      escalated_from: "luna"
                    }
                  }
                ]
              }
            }} = Client.snapshot()
  end

  test "orchestrator dispatch eligibility honors the shared intake gate" do
    state = %Orchestrator.State{
      max_concurrent_agents: 2,
      running: %{},
      claimed: MapSet.new(),
      blocked: %{},
      codex_totals: %{},
      retry_attempts: %{}
    }

    issue = %Issue{
      id: "owner-control-401",
      identifier: "GH-401",
      title: "Ready work",
      state: "Todo",
      labels: [],
      dispatchable: true
    }

    Application.put_env(:symphony_elixir, :owner_control_client_module, PausedControl)
    refute Orchestrator.should_dispatch_issue_for_test(issue, state)

    Application.put_env(:symphony_elixir, :owner_control_client_module, ActiveControl)
    assert Orchestrator.should_dispatch_issue_for_test(issue, state)
  end

  defp configure_client do
    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: "http://127.0.0.1:4080",
      token: String.duplicate("c", 32)
    })
  end
end
