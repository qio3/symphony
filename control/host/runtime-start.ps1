$ErrorActionPreference = "Stop"

. "$PSScriptRoot\common.ps1"

Assert-SymphonyReleasePreflight -Release $script:SymphonyRelease -RuntimeRoot $script:SymphonyRuntimeRoot

if (-not (Test-Path -LiteralPath "C:\Users\teren\.codex\auth.json" -PathType Leaf)) {
    throw "Codex ChatGPT auth is missing"
}
if (-not (Test-Path -LiteralPath $script:SymphonyRuntimeWorkflowFile -PathType Leaf)) {
    throw "Runtime WORKFLOW is missing"
}

& "C:\tools\symphony-owner-control\start.ps1"

Invoke-WithSymphonyReleaseEnvironment -Release $script:SymphonyRelease -Action {
    Invoke-WithSymphonyComposeConfig {
        docker compose -f $script:SymphonyComposeFile up -d --build
    }
}
