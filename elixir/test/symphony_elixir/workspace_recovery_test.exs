defmodule SymphonyElixir.WorkspaceRecoveryTest do
  use SymphonyElixir.TestSupport

  test "partial git metadata without user files is archived outside the active root" do
    test_root =
      Path.join(
        System.tmp_dir!(),
        "symphony-workspace-recovery-#{System.unique_integer([:positive])}"
      )

    workspace_root = Path.join(test_root, "workspaces")
    workspace = Path.join(workspace_root, "GH-617")

    try do
      File.mkdir_p!(Path.join(workspace, ".git"))
      File.write!(Path.join(workspace, ".git/HEAD"), "ref: refs/heads/partial\n")

      write_workflow_file!(Workflow.workflow_file_path(), workspace_root: workspace_root)

      issue = %Issue{id: "617", identifier: "GH-617", title: "Recover workspace"}

      assert {:ok, {:rebuild, archived_workspace}} =
               Workspace.recover_issue_workspace(issue)

      refute File.exists?(workspace)
      assert Path.dirname(archived_workspace) == Path.join(test_root, "workspaces-quarantine")

      assert File.read!(Path.join(archived_workspace, ".git/HEAD")) ==
               "ref: refs/heads/partial\n"
    after
      File.rm_rf(test_root)
    end
  end

  test "clean stale branch already contained in canonical stops before a rebuild" do
    {test_root, workspace_root, workspace, issue} = git_workspace_fixture!("integrated")

    try do
      git!(workspace, ["update-ref", "refs/remotes/origin/rebrand/stanina", "HEAD"])

      write_workflow_file!(Workflow.workflow_file_path(), workspace_root: workspace_root)

      assert {:ok, {:already_integrated, ^workspace}} =
               Workspace.recover_issue_workspace(issue)

      assert File.dir?(workspace)
    after
      File.rm_rf(test_root)
    end
  end

  test "dirty stale branch is preserved with a concrete reason" do
    {test_root, workspace_root, workspace, issue} = git_workspace_fixture!("dirty")

    try do
      git!(workspace, ["update-ref", "refs/remotes/origin/rebrand/stanina", "HEAD"])
      File.write!(Path.join(workspace, "owner-notes.txt"), "preserve me\n")

      write_workflow_file!(Workflow.workflow_file_path(), workspace_root: workspace_root)

      assert {:error, {:workspace_preservation_required, ^workspace, reason}} =
               Workspace.recover_issue_workspace(issue)

      assert reason =~ "uncommitted"
      assert File.read!(Path.join(workspace, "owner-notes.txt")) == "preserve me\n"
    after
      File.rm_rf(test_root)
    end
  end

  test "clean stale branch with unique commits is preserved" do
    {test_root, workspace_root, workspace, issue} = git_workspace_fixture!("unique")

    try do
      git!(workspace, ["update-ref", "refs/remotes/origin/rebrand/stanina", "HEAD"])
      File.write!(Path.join(workspace, "unique.txt"), "unique work\n")
      git!(workspace, ["add", "unique.txt"])
      git!(workspace, ["commit", "-m", "unique work"])

      write_workflow_file!(Workflow.workflow_file_path(), workspace_root: workspace_root)

      assert {:error, {:workspace_preservation_required, ^workspace, reason}} =
               Workspace.recover_issue_workspace(issue)

      assert reason =~ "unique commits"
      assert File.read!(Path.join(workspace, "unique.txt")) == "unique work\n"
    after
      File.rm_rf(test_root)
    end
  end

  test "agent runner reconciles an already integrated stale branch without starting Codex" do
    {test_root, workspace_root, workspace, issue} = git_workspace_fixture!("runner-integrated")

    try do
      git!(workspace, ["update-ref", "refs/remotes/origin/rebrand/stanina", "HEAD"])

      write_workflow_file!(Workflow.workflow_file_path(),
        workspace_root: workspace_root,
        hook_before_run: "printf 'unexpected Symphony branch' >&2; exit 2"
      )

      assert :ok = AgentRunner.run(issue)
      assert File.dir?(Path.join(workspace, ".git"))
    after
      File.rm_rf(test_root)
    end
  end

  test "agent runner leaves a dirty stale branch in place for system quarantine" do
    {test_root, workspace_root, workspace, issue} = git_workspace_fixture!("runner-dirty")

    try do
      git!(workspace, ["update-ref", "refs/remotes/origin/rebrand/stanina", "HEAD"])
      File.write!(Path.join(workspace, "owner-notes.txt"), "preserve me\n")

      write_workflow_file!(Workflow.workflow_file_path(),
        workspace_root: workspace_root,
        hook_before_run: "printf 'unexpected Symphony branch' >&2; exit 2"
      )

      assert {:workspace_hook_failed, "before_run", 2, output} = AgentRunner.run(issue)
      assert output =~ "workspace preservation required"
      assert output =~ workspace
      assert File.read!(Path.join(workspace, "owner-notes.txt")) == "preserve me\n"
    after
      File.rm_rf(test_root)
    end
  end

  defp git_workspace_fixture!(suffix) do
    test_root =
      Path.join(
        System.tmp_dir!(),
        "symphony-workspace-recovery-#{suffix}-#{System.unique_integer([:positive])}"
      )

    workspace_root = Path.join(test_root, "workspaces")
    workspace = Path.join(workspace_root, "GH-617")
    File.mkdir_p!(workspace)

    git!(workspace, ["init", "-b", "rebrand/stanina"])
    git!(workspace, ["config", "user.name", "Symphony Test"])
    git!(workspace, ["config", "user.email", "symphony@example.invalid"])
    File.write!(Path.join(workspace, "README.md"), "base\n")
    git!(workspace, ["add", "README.md"])
    git!(workspace, ["commit", "-m", "base"])
    git!(workspace, ["switch", "-c", "codex/issue-617-symphony-fix"])

    issue = %Issue{id: "617", identifier: "GH-617", title: "Recover workspace"}
    {test_root, workspace_root, workspace, issue}
  end

  defp git!(workspace, args) do
    case System.cmd("git", args, cd: workspace, stderr_to_stdout: true) do
      {_output, 0} -> :ok
      {output, status} -> flunk("git #{Enum.join(args, " ")} failed (#{status}): #{output}")
    end
  end
end
