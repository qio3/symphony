. "C:\tools\symphony-release\release.ps1"

$script:SymphonyRelease = Get-SymphonyReleaseMetadata -Path "C:\tools\symphony-release\release.json"
$script:SymphonyPinnedSha = $script:SymphonyRelease.symphony_sha
