defmodule SymphonyElixir.OwnerControlTest do
  use SymphonyElixir.TestSupport

  import Phoenix.ConnTest
  import Phoenix.LiveViewTest

  alias SymphonyElixir.Orchestrator
  alias SymphonyElixir.OwnerControl.Client
  alias SymphonyElixir.Tracker.Issue

  @endpoint SymphonyElixirWeb.Endpoint

  defmodule FakeDashboardControl do
    def snapshot do
      {:ok, Application.fetch_env!(:symphony_elixir, :owner_control_test_snapshot)}
    end

    def action(action, params) do
      send(Application.fetch_env!(:symphony_elixir, :owner_control_test_pid), {:control_action, action, params})
      {:ok, %{status: "accepted"}}
    end

    def enabled?, do: true
  end

  defmodule PausedControl do
    def intake_active?, do: false
  end

  defmodule ActiveControl do
    def intake_active?, do: true
  end

  defmodule UnavailableControl do
    def snapshot, do: {:error, :econnrefused}
    def action(_action, _params), do: {:error, :econnrefused}
    def enabled?, do: true
  end

  setup do
    keys = [
      :owner_control_settings,
      :owner_control_request_fun,
      :owner_control_client_module,
      :owner_control_test_snapshot,
      :owner_control_test_pid
    ]

    previous = Map.new(keys, &{&1, Application.get_env(:symphony_elixir, &1)})
    endpoint_config = Application.get_env(:symphony_elixir, SymphonyElixirWeb.Endpoint, [])

    on_exit(fn ->
      Enum.each(previous, fn {key, value} ->
        if is_nil(value),
          do: Application.delete_env(:symphony_elixir, key),
          else: Application.put_env(:symphony_elixir, key, value)
      end)

      Application.put_env(:symphony_elixir, SymphonyElixirWeb.Endpoint, endpoint_config)
    end)

    :ok
  end

  test "client reads the authenticated shared snapshot and only allows typed actions" do
    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: "http://127.0.0.1:4081",
      token: String.duplicate("c", 32)
    })

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

    assert_receive {:request, :get, "http://127.0.0.1:4081/v1/snapshot", headers, nil}
    assert {"authorization", "Bearer " <> _token} = List.keyfind(headers, "authorization", 0)

    assert {:ok, %{status: "accepted"}} = Client.action(:run, %{issue: 401})
    assert_receive {:request, :post, "http://127.0.0.1:4081/v1/actions/run", _headers, %{issue: 401}}

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
    assert_receive {:request, :get, "http://127.0.0.1:4081/v1/intake", _headers, nil}
  end

  test "intake is open when control is disabled and fail-closed when configured control is unavailable" do
    Application.delete_env(:symphony_elixir, :owner_control_settings)
    assert Client.intake_active?()

    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: "http://127.0.0.1:4081",
      token: String.duplicate("c", 32)
    })

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      {:error, :econnrefused}
    end)

    refute Client.intake_active?()
  end

  test "client normalizes deterministic model routing fields from the shared JSON snapshot" do
    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: "http://127.0.0.1:4081",
      token: String.duplicate("c", 32)
    })

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      {:ok, 200,
       Jason.encode!(%{
         "version" => 1,
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

  test "dashboard preserves owner layout while exposing shared controls and lifecycle actions" do
    Application.put_env(:symphony_elixir, :owner_control_client_module, FakeDashboardControl)
    Application.put_env(:symphony_elixir, :owner_control_test_pid, self())
    Application.put_env(:symphony_elixir, :owner_control_test_snapshot, control_snapshot())
    start_test_endpoint()

    {:ok, view, html} = live(build_conn(), "/")

    assert html =~ "Intake Active"
    assert html =~ "Workers 1/2"
    assert html =~ "Routing Auto"
    assert html =~ "Luna 0"
    assert html =~ "Terra 1"
    assert html =~ "gpt-5.6-terra"
    assert html =~ "Luna → Terra"
    assert html =~ "Claim pending"
    assert html =~ "Ready for AI"
    assert html =~ "Canonical"
    assert html =~ "be44cf15"
    assert html =~ "synced"
    assert html =~ "Pause intake"
    assert html =~ "Restart service"
    assert html =~ "Accept"
    assert html =~ "Rework"
    assert html =~ "Start"
    assert html =~ "Open Issue"
    assert has_element?(view, "details.runtime-details > summary", "Runtime diagnostics")
    assert has_element?(view, "details.runtime-details section.runtime-section h2", "Running sessions")
    refute has_element?(view, "details.runtime-details[open]")

    view |> element(~s(button[phx-click="pause-intake"])) |> render_click()
    assert_receive {:control_action, :pause, %{}}

    view |> element(~s(button[phx-click="accept"][phx-value-issue="402"])) |> render_click()
    assert_receive {:control_action, :accept, %{issue: 402}}

    view |> form(~s(form[phx-submit="rework"]), %{"issue" => "402", "reason" => "Keep legacy IDs"}) |> render_submit()
    assert_receive {:control_action, :rework, %{issue: 402, reason: "Keep legacy IDs"}}

    view |> element(~s(button[phx-click="run"][phx-value-issue="404"])) |> render_click()
    assert_receive {:control_action, :run, %{issue: 404}}

    refreshed =
      control_snapshot()
      |> put_in([:owner_view, :backlog, Access.at(0), :title], "Fresh project snapshot")

    Application.put_env(:symphony_elixir, :owner_control_test_snapshot, refreshed)
    send(view.pid, :control_refresh)

    assert render(view) =~ "Fresh project snapshot"
  end

  defmodule RejectingDashboardControl do
    def snapshot do
      {:ok, Application.fetch_env!(:symphony_elixir, :owner_control_test_snapshot)}
    end

    def action(_action, _params),
      do: {:error, {:owner_control_action_rejected, "TEST has drift"}}

    def enabled?, do: true
  end

  test "dashboard makes a configured control outage explicit and disables controls" do
    Application.put_env(:symphony_elixir, :owner_control_client_module, UnavailableControl)
    start_test_endpoint()

    {:ok, _view, html} = live(build_conn(), "/")

    assert html =~ "Owner controls unavailable"
    assert html =~ "New work is paused fail-closed"
    refute html =~ "Pause intake"
    refute html =~ "Restart service"
  end

  test "dashboard shows the concrete action rejection reason" do
    Application.put_env(:symphony_elixir, :owner_control_client_module, RejectingDashboardControl)
    Application.put_env(:symphony_elixir, :owner_control_test_snapshot, control_snapshot())
    start_test_endpoint()

    {:ok, view, _html} = live(build_conn(), "/")

    html = view |> element(~s(button[phx-click="accept"][phx-value-issue="402"])) |> render_click()

    assert html =~ "Owner action rejected: TEST has drift"
  end

  defp start_test_endpoint do
    endpoint_config =
      :symphony_elixir
      |> Application.get_env(SymphonyElixirWeb.Endpoint, [])
      |> Keyword.merge(server: false, secret_key_base: String.duplicate("s", 64))

    Application.put_env(:symphony_elixir, SymphonyElixirWeb.Endpoint, endpoint_config)
    start_supervised!({SymphonyElixirWeb.Endpoint, []})
  end

  defp control_snapshot do
    %{
      version: 1,
      generated_at: "2026-08-23T10:00:00Z",
      service: %{live: true},
      intake: %{active: true, status: "active"},
      workers: %{running: 1, limit: 2},
      models: %{
        luna: %{active: 0, completed: 4},
        terra: %{active: 1, completed: 2},
        sol: %{active: 0, completed: 1}
      },
      canonical: %{sha: "be44cf15aaaaaaaa", url: "https://example.org/commit/be44cf15"},
      test: %{sha: "be44cf15aaaaaaaa", url: "https://test.example.org", synced: true, drift: false},
      counts: %{
        backlog: 1,
        ready_for_ai: 0,
        running: 1,
        queued: 0,
        retrying: 0,
        blocked: 1,
        ready_for_acceptance: 1,
        done: 70
      },
      running: [],
      retrying: [],
      blocked: [],
      codex_totals: %{input_tokens: 0, output_tokens: 0, total_tokens: 0, seconds_running: 0},
      rate_limits: nil,
      owner_view: %{
        available: true,
        updated_at: "2026-08-23T10:00:00Z",
        counts: %{backlog: 1, blocked: 1, ready_for_acceptance: 1, done: 70},
        blocked: [
          %{
            number: 403,
            issue_identifier: "#403",
            issue_url: "https://example.org/issues/403",
            title: "Owner decision",
            question: "Choose A or B?",
            reason: "Decision required"
          }
        ],
        work_items: [
          %{
            number: 401,
            issue_identifier: "#401",
            issue_url: "https://example.org/issues/401",
            title: "Work",
            stage: "In Progress",
            status: "running",
            started_at: "2026-08-23T09:45:00Z",
            model: %{
              selected_tier: "terra",
              actual_model: "gpt-5.6-terra",
              routing_reason: "escalation:max_turns_exhausted",
              escalated_from: "luna",
              escalation_history: [
                %{from: "luna", to: "terra", reason: "max_turns_exhausted"}
              ]
            },
            pr: nil,
            ci: nil,
            test: nil
          },
          %{
            number: 405,
            issue_identifier: "#405",
            issue_url: "https://example.org/issues/405",
            title: "Claim pending",
            stage: "In Progress",
            status: "In Progress",
            model: nil,
            pr: nil,
            ci: nil
          }
        ],
        ready_for_acceptance: [
          %{
            number: 402,
            issue_identifier: "#402",
            issue_url: "https://example.org/issues/402",
            title: "Ready",
            stage: "Ready for Acceptance",
            status: "ready",
            started_at: nil,
            pr: %{number: 99, url: "https://example.org/pull/99"},
            ci: %{status: "success", url: "https://example.org/pull/99/checks"},
            test: %{sha: "be44cf15aaaaaaaa", url: "https://test.example.org"}
          }
        ],
        backlog: [
          %{
            number: 404,
            issue_identifier: "#404",
            issue_url: "https://example.org/issues/404",
            title: "Next",
            stage: "Backlog",
            status: "backlog",
            started_at: nil,
            pr: nil,
            ci: nil,
            test: nil
          }
        ]
      }
    }
  end
end
