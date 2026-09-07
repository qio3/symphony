$ErrorActionPreference = "Stop"

function Get-SymphonyReleaseMetadata {
    param(
        [string]$Path = (Join-Path $PSScriptRoot "release.json")
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Symphony release metadata is missing: $Path"
    }

    $release = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    $sha = [string]$release.symphony_sha
    $codexVersion = [string]$release.codex_version

    if ($sha -notmatch '^[0-9a-f]{40}$') {
        throw "Symphony release SHA must be a full lowercase commit SHA"
    }
    if ($codexVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "Codex version must use x.y.z format"
    }

    return [pscustomobject]@{
        symphony_sha = $sha
        codex_version = $codexVersion
    }
}

function Assert-SymphonyReleasePreflight {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot
    )

    $actualSha = [string](& git -C $RuntimeRoot rev-parse HEAD)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Symphony runtime HEAD"
    }
    $actualSha = $actualSha.Trim()
    if ($actualSha -ne $Release.symphony_sha) {
        throw "Symphony runtime is not pinned to $($Release.symphony_sha) (got $actualSha)"
    }

    $runtimeChanges = & git -C $RuntimeRoot status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to verify Symphony runtime checkout"
    }
    if ($runtimeChanges) {
        throw "Symphony runtime checkout has local changes"
    }
}

function Invoke-WithSymphonyReleaseEnvironment {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $previousSha = $env:SYMPHONY_RELEASE_SHA
    $previousCodexVersion = $env:SYMPHONY_CODEX_VERSION
    try {
        $env:SYMPHONY_RELEASE_SHA = $Release.symphony_sha
        $env:SYMPHONY_CODEX_VERSION = $Release.codex_version
        & $Action
    }
    finally {
        if ($null -eq $previousSha) {
            Remove-Item Env:SYMPHONY_RELEASE_SHA -ErrorAction SilentlyContinue
        }
        else {
            $env:SYMPHONY_RELEASE_SHA = $previousSha
        }
        if ($null -eq $previousCodexVersion) {
            Remove-Item Env:SYMPHONY_CODEX_VERSION -ErrorAction SilentlyContinue
        }
        else {
            $env:SYMPHONY_CODEX_VERSION = $previousCodexVersion
        }
    }
}
