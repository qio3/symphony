# Host release metadata

These templates keep the selected Symphony runtime SHA and Codex CLI version in one non-secret
file. Install `release.ps1` and `release.json` in `C:\tools\symphony-release`, then fold the two
small `*-common.ps1` snippets into the existing runtime and Owner Control launchers. Apply
`docker-compose.release.yml` as the release section of the existing Compose file and use the
preflight order from `runtime-start.ps1`.

To update or roll back Symphony, change only `release.json`, check out that exact SHA in the clean
runtime source, and run the normal start command. The shared helper validates the selected value;
the launchers never infer a trusted pin from the current checkout.

All checkout, auth, workflow and other read-only preflight checks must finish before Owner Control
or Docker starts. The existing ports, mounts, secrets, worker limit and ChatGPT-only auth remain
owned by the host configuration and are not duplicated here.
