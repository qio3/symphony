defmodule SymphonyElixir.WorkflowUnicodeTest do
  use ExUnit.Case, async: true

  test "loading and rendering preserve UTF-8 with LF and CRLF" do
    path = Path.join(System.tmp_dir!(), "workflow-unicode-#{System.unique_integer([:positive])}.md")
    on_exit(fn -> File.rm(path) end)
    prompt = "Исходных событий…\nЖдать завершения задачи {{ issue.id }}."

    for newline <- ["\n", "\r\n"] do
      content = String.replace("---\n{}\n---\n" <> prompt, "\n", newline)
      File.write!(path, content)
      assert {:ok, workflow} = SymphonyElixir.Workflow.load(path)
      assert String.valid?(workflow.prompt_template)
      assert workflow.prompt_template == prompt

      rendered =
        workflow.prompt_template
        |> Solid.parse!()
        |> Solid.render!(%{"issue" => %{"id" => 1033}})
        |> IO.iodata_to_binary()

      assert String.valid?(rendered)
      assert Jason.decode!(Jason.encode!(%{text: rendered}))["text"] == rendered
    end
  end
end
