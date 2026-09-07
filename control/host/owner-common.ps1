. "C:\tools\symphony-release\release.ps1"

$script:SymphonyRelease = Get-SymphonyReleaseMetadata -Path "C:\tools\symphony-release\release.json"
$script:ExpectedSymphonySha = $script:SymphonyRelease.symphony_sha
