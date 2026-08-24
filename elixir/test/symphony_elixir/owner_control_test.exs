defmodule SymphonyElixir.OwnerControlTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.Orchestrator
  alias SymphonyElixir.OwnerControl.Client
  alias SymphonyElixir.Tracker.Issue

  setup_all do
    runtime_was_running = is_pid(Process.whereis(SymphonyElixir.AgentRuntimeSupervisor))

    if runtime_was_running do
      :ok =
        Supervisor.terminate_child(
          SymphonyElixir.Supervisor,
          SymphonyElixir.AgentRuntimeSupervisor
        )
    end

    on_exit(fn ->
      Workflow.clear_workflow_file_path()

      if runtime_was_running and is_nil(Process.whereis(SymphonyElixir.AgentRuntimeSupervisor)) do
        case Supervisor.restart_child(
               SymphonyElixir.Supervisor,
               SymphonyElixir.AgentRuntimeSupervisor
             ) do
          {:ok, _pid} -> :ok
          {:error, {:already_started, _pid}} -> :ok
        end
      end
    end)

    :ok
  end

  defmodule PausedControl do
    def intake_active?, do: false
  end

  defmodule ActiveControl do
    def intake_active?, do: true
  end

  defmodule DispatchControl do
    def snapshot do
      if test_orchestrator?() do
        test_pid = Application.fetch_env!(:symphony_elixir, :owner_control_test_pid)
        cycle_ref = Application.fetch_env!(:symphony_elixir, :owner_control_test_cycle_ref)
        send(test_pid, {:owner_control_snapshot, cycle_ref})
        Application.fetch_env!(:symphony_elixir, :owner_control_test_snapshot)
      else
        {:error, :outside_owner_control_dispatch_test}
      end
    end

    def action(:lease, %{issue: issue_number} = params) do
      unless test_orchestrator?(), do: raise("unexpected lease caller")

      test_pid = Application.fetch_env!(:symphony_elixir, :owner_control_test_pid)
      cycle_ref = Application.fetch_env!(:symphony_elixir, :owner_control_test_cycle_ref)
      response = Application.fetch_env!(:symphony_elixir, :owner_control_test_action_response)

      send(test_pid, {:owner_control_action, cycle_ref, :lease, params})

      if match?({:ok, _}, response) do
        issues = Application.fetch_env!(:symphony_elixir, :memory_tracker_issues)
        issue_id = Integer.to_string(issue_number)

        Application.put_env(
          :symphony_elixir,
          :memory_tracker_issues,
          Enum.map(issues, fn
            %Issue{id: ^issue_id} = issue ->
              %{issue | labels: Enum.uniq(["symphony" | issue.labels])}

            issue ->
              issue
          end)
        )
      end

      response
    end

    def intake_active? do
      test_orchestrator?() and
        Application.fetch_env!(:symphony_elixir, :owner_control_test_intake_active)
    end

    defp test_orchestrator? do
      Process.info(self(), :registered_name) ==
        {:registered_name, Application.get_env(:symphony_elixir, :owner_control_test_orchestrator_name)}
    end
  end

  setup do
    keys = [
      :owner_control_settings,
      :owner_control_request_fun,
      :owner_control_client_module,
      :owner_control_test_pid,
      :owner_control_test_cycle_ref,
      :owner_control_test_snapshot,
      :owner_control_test_action_response,
      :owner_control_test_intake_active,
      :owner_control_test_orchestrator_name
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

    assert {:ok, %{status: "accepted"}} = Client.action(:lease, %{issue: 401})
    assert_receive {:request, :post, "http://127.0.0.1:4080/v1/actions/lease", _headers, %{issue: 401}}

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

  test "disabled, invalid configuration, and request failures fail closed" do
    Application.delete_env(:symphony_elixir, :owner_control_settings)
    refute Client.enabled?()
    assert Client.snapshot() == :disabled
    assert Client.action(:pause, %{}) == {:error, :owner_control_disabled}

    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: "file:///not-an-owner-control-endpoint",
      token: "too-short"
    })

    assert Client.enabled?()
    assert Client.snapshot() == {:error, :invalid_owner_control_settings}
    assert Client.action(:pause, %{}) == {:error, :invalid_owner_control_settings}
    refute Client.intake_active?()

    configure_client()

    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ ->
      raise "offline"
    end)

    assert {:error, {:owner_control_request_failed, "offline"}} = Client.snapshot()
    refute Client.intake_active?()
  end

  test "default Req client crosses the loopback HTTP boundary with auth and JSON" do
    Application.delete_env(:symphony_elixir, :owner_control_request_fun)

    token = String.duplicate("t", 32)

    {port, server} =
      start_loopback_server(
        [
          {200, ~s({"version":1,"intake":{"active":true}})},
          {202, ~s({"status":"accepted"})}
        ],
        self()
      )

    on_exit(fn ->
      if Process.alive?(server), do: Process.exit(server, :kill)
    end)

    monitor = Process.monitor(server)
    configure_client("http://127.0.0.1:#{port}", token)

    assert {:ok, %{version: 1, intake: %{active: true}}} = Client.snapshot()
    assert_receive {:owner_control_http_request, snapshot_request}, 1_000
    assert String.starts_with?(snapshot_request.head, "GET /v1/snapshot HTTP/1.1")
    assert String.contains?(String.downcase(snapshot_request.head), "authorization: bearer #{token}")

    assert {:ok, %{status: "accepted"}} = Client.action(:pause, %{})
    assert_receive {:owner_control_http_request, action_request}, 1_000
    assert String.starts_with?(action_request.head, "POST /v1/actions/pause HTTP/1.1")
    assert String.contains?(String.downcase(action_request.head), "content-type: application/json")
    assert Jason.decode!(action_request.body) == %{}

    assert_receive {:DOWN, ^monitor, :process, ^server, :normal}, 1_000
    assert {:error, {:owner_control_unavailable, _reason}} = Client.snapshot()
  end

  test "malformed owner control responses remain errors" do
    configure_client()

    stub_owner_response({:ok, 200, "not json"})
    assert {:error, {:invalid_owner_control_json, _}} = Client.snapshot()

    stub_owner_response({:ok, 503, %{"error" => %{}}})
    refute Client.intake_active?()
  end

  test "owner control response decoding is deterministic and fail-closed" do
    configure_client()

    stub_owner_response({:ok, 200, %{"version" => 1, "future" => %{42 => "value"}}})
    assert {:ok, %{"future" => %{42 => "value"}, version: 1}} = Client.snapshot()

    stub_owner_response({:ok, 200, "[]"})
    assert {:error, :invalid_owner_control_payload} = Client.snapshot()

    stub_owner_response({:ok, 200, 42})
    assert {:error, :invalid_owner_control_payload} = Client.snapshot()

    stub_owner_response({:ok, 503, %{error: %{message: "runtime stopped"}}})

    assert {:error, {:owner_control_action_rejected, "runtime stopped"}} =
             Client.action(:restart, %{})

    stub_owner_response({:ok, 409, "not json"})
    assert {:error, {:owner_control_http_error, 409}} = Client.action(:accept, %{issue: 401})

    stub_owner_response({:ok, 503, %{"error" => %{}}})
    assert {:error, {:owner_control_http_error, 503}} = Client.action(:pause, %{})

    stub_owner_response({:ok, 500, %{error: %{message: "ignored for generic status"}}})
    assert {:error, {:owner_control_http_error, 500}} = Client.snapshot()
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

  test "fresh Ready for AI issues acquire their durable leases from one snapshot before dispatch" do
    issues = [dispatch_issue(401), dispatch_issue(402)]

    snapshot =
      dispatch_snapshot([
        %{number: 401, status: "Ready for AI", state: "OPEN"},
        %{number: 402, status: "Ready for AI", state: "OPEN"}
      ])

    {pid, task_supervisor, cycle_ref, test_root} =
      start_dispatch_cycle(issues, snapshot, {:ok, %{status: "accepted"}})

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    assert_receive {:owner_control_snapshot, ^cycle_ref}, 1_000
    assert_receive {:owner_control_action, ^cycle_ref, :lease, %{issue: 401}}, 1_000
    assert_receive {:owner_control_action, ^cycle_ref, :lease, %{issue: 402}}, 1_000

    assert eventually(fn -> map_size(:sys.get_state(pid).running) == 2 end)
    refute_receive {:owner_control_snapshot, ^cycle_ref}, 100
  end

  test "labeled issues keep the existing dispatch path without reacquiring the lease" do
    issue = %{dispatch_issue(403) | labels: ["symphony"]}
    snapshot = dispatch_snapshot([], stale: true, github_status: "unavailable")

    {pid, task_supervisor, cycle_ref, test_root} =
      start_dispatch_cycle([issue], snapshot, {:ok, %{status: "accepted"}})

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    assert_receive {:owner_control_snapshot, ^cycle_ref}, 1_000
    assert eventually(fn -> Map.has_key?(:sys.get_state(pid).running, "403") end)
    refute_receive {:owner_control_action, ^cycle_ref, :lease, _params}, 100
  end

  test "labeled issues keep dispatching when readiness snapshot is unavailable but intake is active" do
    issue = %{dispatch_issue(409) | labels: ["symphony"]}

    {pid, task_supervisor, cycle_ref, test_root} =
      start_dispatch_cycle([issue], %{}, {:ok, %{status: "accepted"}}, snapshot_response: {:error, :owner_control_unavailable})

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    assert_receive {:owner_control_snapshot, ^cycle_ref}, 1_000
    assert eventually(fn -> Map.has_key?(:sys.get_state(pid).running, "409") end)
    refute_receive {:owner_control_action, ^cycle_ref, :lease, _params}, 100
  end

  test "unleased issues fail closed outside a fresh active Ready for AI snapshot" do
    cases = [
      {:backlog, dispatch_snapshot([%{number: 404, status: "Backlog", state: "OPEN"}])},
      {:blocked, dispatch_snapshot([%{number: 404, status: "Blocked", state: "OPEN"}])},
      {:acceptance, dispatch_snapshot([%{number: 404, status: "Ready for Acceptance", state: "OPEN"}])},
      {:done, dispatch_snapshot([%{number: 404, status: "Done", state: "CLOSED"}])},
      {:stale, dispatch_snapshot([%{number: 404, status: "Ready for AI", state: "OPEN"}], stale: true)},
      {:github_unavailable,
       dispatch_snapshot([%{number: 404, status: "Ready for AI", state: "OPEN"}],
         github_status: "unavailable"
       )},
      {:missing_number, dispatch_snapshot([%{status: "Ready for AI", state: "OPEN"}])},
      {:non_numeric_number, dispatch_snapshot([%{number: "GH-404", status: "Ready for AI", state: "OPEN"}])}
    ]

    mismatched_key =
      dispatch_snapshot([%{number: 404, status: "Ready for AI", state: "OPEN"}])
      |> put_in([:issues], %{"999" => %{number: 404, status: "Ready for AI", state: "OPEN"}})

    malformed_key =
      dispatch_snapshot([%{number: 404, status: "Ready for AI", state: "OPEN"}])
      |> put_in([:issues], %{"#404" => %{number: 404, status: "Ready for AI", state: "OPEN"}})

    cases = cases ++ [{:mismatched_key, mismatched_key}, {:malformed_key, malformed_key}]

    Enum.each(cases, fn {case_name, snapshot} ->
      {pid, task_supervisor, cycle_ref, test_root} =
        start_dispatch_cycle([dispatch_issue(404)], snapshot, {:ok, %{status: "accepted"}})

      assert_receive {:owner_control_snapshot, ^cycle_ref}, 1_000, "missing snapshot for #{case_name}"
      assert :sys.get_state(pid).running == %{}

      refute_receive {:owner_control_action, ^cycle_ref, :lease, _params},
                     25,
                     "unexpected action for #{case_name}"

      stop_dispatch_cycle(pid, task_supervisor, test_root)
    end)
  end

  test "a rejected lease action does not dispatch an unleased Ready for AI issue" do
    issue = dispatch_issue(405)
    snapshot = dispatch_snapshot([%{number: 405, status: "Ready for AI", state: "OPEN"}])

    {pid, task_supervisor, cycle_ref, test_root} =
      start_dispatch_cycle(
        [issue],
        snapshot,
        {:error, {:owner_control_action_rejected, "issue moved"}}
      )

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    assert_receive {:owner_control_snapshot, ^cycle_ref}, 1_000
    assert_receive {:owner_control_action, ^cycle_ref, :lease, %{issue: 405}}, 1_000
    assert :sys.get_state(pid).running == %{}
  end

  test "authoritative paused intake blocks a fresh cached Ready for AI snapshot" do
    snapshot =
      dispatch_snapshot([%{number: 407, status: "Ready for AI", state: "OPEN"}],
        intake_active: true
      )

    {pid, task_supervisor, cycle_ref, test_root} =
      start_dispatch_cycle([dispatch_issue(407)], snapshot, {:ok, %{status: "accepted"}}, intake_active: false)

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    refute_receive {:owner_control_snapshot, ^cycle_ref}, 100
    assert :sys.get_state(pid).running == %{}
    refute_receive {:owner_control_action, ^cycle_ref, :lease, _params}, 100
  end

  test "disabled Owner Control leaves generic label routing unchanged" do
    Application.delete_env(:symphony_elixir, :owner_control_settings)
    Application.delete_env(:symphony_elixir, :owner_control_client_module)

    issue = %{dispatch_issue(406) | labels: ["symphony"]}

    {pid, task_supervisor, _cycle_ref, test_root} =
      start_dispatch_cycle([issue], :disabled, {:ok, %{status: "accepted"}}, configure_control: false)

    on_exit(fn -> stop_dispatch_cycle(pid, task_supervisor, test_root) end)

    assert eventually(fn -> Map.has_key?(:sys.get_state(pid).running, "406") end)

    unlabeled = dispatch_issue(408)

    {unlabeled_pid, unlabeled_supervisor, _cycle_ref, unlabeled_root} =
      start_dispatch_cycle([unlabeled], :disabled, {:ok, %{status: "accepted"}}, configure_control: false)

    on_exit(fn ->
      stop_dispatch_cycle(unlabeled_pid, unlabeled_supervisor, unlabeled_root)
    end)

    assert :sys.get_state(unlabeled_pid).running == %{}
  end

  defp configure_client(url \\ "http://127.0.0.1:4080", token \\ String.duplicate("c", 32)) do
    Application.put_env(:symphony_elixir, :owner_control_settings, %{
      url: url,
      token: token
    })
  end

  defp stub_owner_response(response) do
    Application.put_env(:symphony_elixir, :owner_control_request_fun, fn _, _, _, _ -> response end)
  end

  defp dispatch_issue(number) do
    %Issue{
      id: Integer.to_string(number),
      identifier: "GH-#{number}",
      title: "Ready work #{number}",
      description: "Test dispatch",
      state: "open",
      url: "https://github.test/issues/#{number}",
      labels: [],
      dispatchable: true
    }
  end

  defp dispatch_snapshot(items, opts \\ []) do
    %{
      stale: Keyword.get(opts, :stale, false),
      intake: %{active: Keyword.get(opts, :intake_active, true)},
      sources: %{github: %{status: Keyword.get(opts, :github_status, "fresh")}},
      issues:
        Map.new(items, fn item ->
          key = if is_integer(item[:number]), do: Integer.to_string(item.number), else: inspect(item[:number])
          {key, item}
        end)
    }
  end

  defp start_dispatch_cycle(issues, snapshot, action_response, opts \\ []) do
    cycle_ref = make_ref()
    test_root = Path.join(System.tmp_dir!(), "symphony-owner-dispatch-#{System.unique_integer([:positive])}")
    orchestrator_name = Module.concat(__MODULE__, "OwnerDispatch#{System.unique_integer([:positive])}")

    write_workflow_file!(Workflow.workflow_file_path(),
      tracker_kind: "memory",
      tracker_required_labels: ["symphony"],
      tracker_active_states: ["open"],
      poll_interval_ms: 60_000,
      workspace_root: test_root,
      hook_before_run: "sleep 30"
    )

    Application.put_env(:symphony_elixir, :memory_tracker_issues, issues)
    {:ok, task_supervisor} = Task.Supervisor.start_link()
    Process.unlink(task_supervisor)

    if Keyword.get(opts, :configure_control, true) do
      Application.put_env(:symphony_elixir, :owner_control_client_module, DispatchControl)
      Application.put_env(:symphony_elixir, :owner_control_test_pid, self())
      Application.put_env(:symphony_elixir, :owner_control_test_cycle_ref, cycle_ref)

      Application.put_env(
        :symphony_elixir,
        :owner_control_test_snapshot,
        Keyword.get(opts, :snapshot_response, {:ok, snapshot})
      )

      Application.put_env(:symphony_elixir, :owner_control_test_action_response, action_response)

      Application.put_env(
        :symphony_elixir,
        :owner_control_test_intake_active,
        Keyword.get(opts, :intake_active, true)
      )

      Application.put_env(
        :symphony_elixir,
        :owner_control_test_orchestrator_name,
        orchestrator_name
      )
    end

    {:ok, pid} =
      Orchestrator.start_link(
        name: orchestrator_name,
        task_supervisor: task_supervisor,
        account_rate_limits_reader: fn -> {:error, :test_disabled} end,
        usage_ledger_path: Path.join(test_root, "usage-ledger.json")
      )

    Process.unlink(pid)
    {pid, task_supervisor, cycle_ref, test_root}
  end

  defp stop_dispatch_cycle(pid, task_supervisor, test_root) do
    if Process.alive?(pid), do: GenServer.stop(pid)
    if Process.alive?(task_supervisor), do: Supervisor.stop(task_supervisor)
    File.rm_rf(test_root)
  end

  defp eventually(fun, attempts \\ 100)

  defp eventually(fun, attempts) when attempts > 0 do
    if fun.() do
      true
    else
      Process.sleep(10)
      eventually(fun, attempts - 1)
    end
  end

  defp eventually(_fun, 0), do: false

  defp start_loopback_server(responses, parent) do
    {:ok, listen_socket} =
      :gen_tcp.listen(
        0,
        [
          :binary,
          packet: :raw,
          active: false,
          reuseaddr: true,
          ip: {127, 0, 0, 1}
        ]
      )

    {:ok, port} = :inet.port(listen_socket)

    server =
      spawn(fn ->
        receive do
          {:serve, socket, queued_responses, recipient} ->
            Enum.each(queued_responses, fn {status, body} ->
              {:ok, client} = :gen_tcp.accept(socket)
              send(recipient, {:owner_control_http_request, read_http_request(client)})
              :ok = :gen_tcp.send(client, http_response(status, body))
              :ok = :gen_tcp.close(client)
            end)

            :ok = :gen_tcp.close(socket)
        end
      end)

    :ok = :gen_tcp.controlling_process(listen_socket, server)
    send(server, {:serve, listen_socket, responses, parent})
    {port, server}
  end

  defp read_http_request(socket), do: read_http_request(socket, <<>>)

  defp read_http_request(socket, received) do
    case :binary.match(received, "\r\n\r\n") do
      {index, 4} ->
        header_size = index + 4
        <<head::binary-size(header_size), remaining::binary>> = received
        body_size = content_length(head)
        %{head: head, body: read_http_body(socket, remaining, body_size)}

      :nomatch ->
        {:ok, chunk} = :gen_tcp.recv(socket, 0, 1_000)
        read_http_request(socket, received <> chunk)
    end
  end

  defp read_http_body(_socket, body, body_size) when byte_size(body) >= body_size,
    do: binary_part(body, 0, body_size)

  defp read_http_body(socket, body, body_size) do
    {:ok, remaining} = :gen_tcp.recv(socket, body_size - byte_size(body), 1_000)
    body <> remaining
  end

  defp content_length(headers) do
    headers
    |> String.split("\r\n")
    |> Enum.find_value(0, &parse_content_length/1)
  end

  defp parse_content_length(line) do
    case String.split(line, ":", parts: 2) do
      [name, value] -> content_length_value(String.downcase(name), value)
      _other -> nil
    end
  end

  defp content_length_value("content-length", value), do: String.to_integer(String.trim(value))
  defp content_length_value(_name, _value), do: nil

  defp http_response(status, body) do
    [
      "HTTP/1.1 #{status} OK",
      "content-type: application/json",
      "content-length: #{byte_size(body)}",
      "connection: close",
      "",
      body
    ]
    |> Enum.join("\r\n")
  end
end
