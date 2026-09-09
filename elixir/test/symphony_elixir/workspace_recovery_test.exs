defmodule SymphonyElixir.WorkspaceRecoveryTest do
  use SymphonyElixir.TestSupport

  test "partial git metadata is archived on the workspace volume outside issue directories" do
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
      assert String.starts_with?(archived_workspace, Path.join(workspace_root, ".symphony-quarantine") <> "/")

      assert File.read!(Path.join(archived_workspace, ".git/HEAD")) ==
               "ref: refs/heads/partial\n"
    after
      File.rm_rf(test_root)
    end
  end

  test "recovery never replaces an earlier archive" do
    {root, workspace} = partial_fixture!()
    assert {:ok, {:rebuild, first}} = Workspace.recover_issue_workspace("GH-900617")
    File.mkdir_p!(Path.join(workspace, ".git"))
    File.write!(Path.join(workspace, ".git/HEAD"), "second\n")
    assert {:ok, {:rebuild, second}} = Workspace.recover_issue_workspace("GH-900617")
    refute first == second
    assert File.read!(Path.join(first, ".git/HEAD")) == "partial\n"
    assert File.read!(Path.join(second, ".git/HEAD")) == "second\n"
    assert File.dir?(Path.join(root, ".symphony-quarantine"))
  end

  test "quarantine namespace cannot be used, removed or sent to Codex even after root reload" do
    {root, _workspace} = partial_fixture!()
    quarantine = Path.join(root, ".symphony-quarantine")
    archive = Path.join(quarantine, "kept")
    File.mkdir_p!(archive)
    File.write!(Path.join(archive, "evidence"), "preserve")
    marker = Path.join(root, "unexpected-hook")

    write_workflow_file!(Workflow.workflow_file_path(),
      workspace_root: root,
      hook_after_create: "touch #{marker}",
      hook_before_remove: "touch #{marker}",
      codex_command: "touch #{marker}; exit 1"
    )

    assert {:error, _} = Workspace.create_for_issue(".symphony-quarantine")
    assert {:error, _} = Workspace.recover_issue_workspace(".symphony-quarantine")
    issue = %Issue{id: "900617", identifier: "GH-900617", title: "fixture"}

    for path <- [quarantine, archive, Path.join(archive, "missing")] do
      assert {:error, _} = AppServer.run(path, "must not run", issue)
      assert {:error, _, _} = Workspace.remove(path)
      assert {:error, _, _} = Workspace.remove_recorded(path, nil)
    end

    refute File.exists?(marker)
    assert File.read!(Path.join(archive, "evidence")) == "preserve"

    write_workflow_file!(Workflow.workflow_file_path(), workspace_root: Path.join(root, "new-root"))
    assert {:error, _, _} = Workspace.remove_recorded(archive, nil)
    assert File.read!(Path.join(archive, "evidence")) == "preserve"
  end

  for target <- [:inside, :outside, :missing, :file] do
    test "unsafe quarantine destination #{target} preserves the source" do
      {root, workspace} = partial_fixture!()
      quarantine = Path.join(root, ".symphony-quarantine")
      unsafe_archive_fixture!(root, quarantine, unquote(target))

      assert {:error, {:workspace_preservation_required, ^workspace, reason}} =
               Workspace.recover_issue_workspace("GH-900617")

      assert reason =~ "archive"
      assert File.read!(Path.join(workspace, ".git/HEAD")) == "partial\n"
      assert File.lstat!(quarantine).type in [:symlink, :regular]
    end
  end

  test "issue alias into the archive cannot run or clean up archived work" do
    {root, _workspace} = partial_fixture!()
    archive = Path.join([root, ".symphony-quarantine", "kept"])
    File.mkdir_p!(archive)
    File.write!(Path.join(archive, "evidence"), "preserve")
    alias_path = Path.join(root, "GH-900618")
    File.ln_s!(archive, alias_path)
    issue = %Issue{id: "900618", identifier: "GH-900618", title: "fixture"}
    assert {:error, _} = Workspace.create_for_issue(issue)
    assert {:error, _} = Workspace.recover_issue_workspace(issue)
    assert {:error, _} = AppServer.run(alias_path, "must not run", issue)
    assert {:error, _, _} = Workspace.remove(alias_path)
    assert {:error, _, _} = Workspace.remove_recorded(alias_path, nil)
    assert File.read!(Path.join(archive, "evidence")) == "preserve"
  end

  test "invalid git with user files is not archived" do
    {root, workspace} = partial_fixture!()
    File.write!(Path.join(workspace, "notes"), "owner work")

    assert {:error, {:workspace_preservation_required, ^workspace, _}} =
             Workspace.recover_issue_workspace("GH-900617")

    assert File.read!(Path.join(workspace, "notes")) == "owner work"
    refute File.exists?(Path.join(root, ".symphony-quarantine"))
  end

  test "reserved issue identifier cannot alias an ordinary workspace" do
    {root, _workspace} = partial_fixture!()
    target = Path.join(root, "ordinary")
    File.mkdir_p!(target)
    File.write!(Path.join(target, "notes"), "preserve")
    File.ln_s!(target, Path.join(root, ".symphony-quarantine"))
    assert {:error, _} = Workspace.create_for_issue(".symphony-quarantine")
    assert {:error, _} = Workspace.recover_issue_workspace(".symphony-quarantine")
    Workspace.remove_issue_workspaces(".symphony-quarantine")
    assert File.read!(Path.join(target, "notes")) == "preserve"
  end

  test "failed archive stops the runner without bootstrap and includes the archive reason" do
    {root, workspace} = partial_fixture!()
    File.write!(Path.join(root, ".symphony-quarantine"), "not a directory")
    marker = Path.join(root, "unexpected-bootstrap")

    write_workflow_file!(Workflow.workflow_file_path(),
      workspace_root: root,
      hook_before_run: "echo 'not a git repository' >&2; exit 2",
      hook_after_create: "touch #{marker}"
    )

    issue = %Issue{id: "900617", identifier: "GH-900617", title: "fixture"}
    assert {:workspace_hook_failed, "before_run", 2, output} = AgentRunner.run(issue)
    assert output =~ "could not archive workspace"
    refute File.exists?(marker)
    assert File.read!(Path.join(workspace, ".git/HEAD")) == "partial\n"
  end

  defp partial_fixture! do
    base = Path.join(System.tmp_dir!(), "symphony-recovery-boundary-#{System.unique_integer([:positive])}")
    root = Path.join(base, "workspaces")
    workspace = Path.join(root, "GH-900617")
    File.mkdir_p!(Path.join(workspace, ".git"))
    File.write!(Path.join(workspace, ".git/HEAD"), "partial\n")
    write_workflow_file!(Workflow.workflow_file_path(), workspace_root: root)
    on_exit(fn -> File.rm_rf(base) end)
    {root, workspace}
  end

  defp unsafe_archive_fixture!(_root, quarantine, :file),
    do: File.write!(quarantine, "not a directory")

  defp unsafe_archive_fixture!(root, quarantine, target) do
    destination =
      if target == :outside, do: Path.join(Path.dirname(root), "outside"), else: Path.join(root, "target")

    if target != :missing, do: File.mkdir_p!(destination)
    File.ln_s!(destination, quarantine)
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

      assert {:already_integrated, ^workspace} = AgentRunner.run(issue)
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
