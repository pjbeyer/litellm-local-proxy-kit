# check-gate.ps1 - Evaluate implementation gate for a feature
# Usage: check-gate.ps1 <feature-name>
# Output: Gate result JSON per Contract 3

param(
    [Parameter(Mandatory=$false, Position=0)]
    [string]$Feature
)

if ($Feature -cnotmatch '\A[0-9]{3,}-[a-z0-9]+(?:-[a-z0-9]+)*\z') {
    Write-Error "Error: Expected a numbered feature directory name. Usage: check-gate.ps1 <feature-name>"
    exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Read approval state
try {
    $stateJson = & "$ScriptDir/read-approval-state.ps1" $Feature 2>$null
    $readSucceeded = $?
    if (-not $readSucceeded -or [string]::IsNullOrWhiteSpace($stateJson -join '')) { throw 'Missing approval state output' }
    $state = $stateJson | ConvertFrom-Json -ErrorAction Stop
    if ($state -isnot [pscustomobject]) { throw 'Invalid approval state' }
} catch {
    $result = @{
        outcome = "blocked"
        reason = "Failed to read approval state for feature: $Feature"
        actions_required = @("Run /speckit.spec-validate.validate to validate spec.md")
        warnings = @()
    }
    $result | ConvertTo-Json -Depth 3
    exit 1
}

function Output-Gate {
    param($Outcome, $Reason, $Actions, $Warnings)
    $result = @{
        outcome = $Outcome
        reason = $Reason
        actions_required = @(if ($Actions) { $Actions })
        warnings = @(if ($Warnings) { $Warnings })
    }
    $result | ConvertTo-Json -Depth 3
    if ($Outcome -eq "blocked") { exit 1 } else { exit 0 }
}

# Rule 1: No validation
if ($state.spec_self_validation -eq "not-run") {
    Output-Gate "blocked" "Spec has not been validated" @("Run /speckit.spec-validate.validate")
}

# Rule 2: Validation failed
if ($state.spec_self_validation -eq "failed") {
    Output-Gate "blocked" "Spec validation failed" @("Re-run /speckit.spec-validate.validate")
}

# All approval paths require a current validation digest.
$specDir = $env:SPEC_VALIDATE_SPEC_DIR
if (-not $specDir) {
    $dir = Get-Item -LiteralPath (Get-Location).Path
    while ($dir -and -not (Test-Path -LiteralPath (Join-Path $dir.FullName '.specify') -PathType Container)) {
        $dir = $dir.Parent
    }
    if ($dir) { $specDir = Join-Path $dir.FullName "specs/$Feature" }
}
$currentHash = $null
if ($specDir) {
    $currentHash = & "$ScriptDir/compute-hash.ps1" (Join-Path $specDir 'spec.md') 2>$null
    if ($LASTEXITCODE -ne 0) { $currentHash = $null }
}
if ($currentHash -cnotmatch '^sha256:[0-9a-f]{64}$' -or $currentHash -cne $state.spec_hash) {
    Output-Gate "blocked" "Current spec digest is unavailable or does not match validation" @("Re-run /speckit.spec-validate.validate with current spec.md")
}

# Task comprehension is independent of spec comprehension.
$currentTasksHash = & "$ScriptDir/compute-hash.ps1" (Join-Path $specDir 'tasks.md') 2>$null
if ($LASTEXITCODE -ne 0 -or $state.tasks_self_validation -isnot [string] -or
    $state.tasks_self_validation -cne 'passed' -or $state.tasks_hash -isnot [string] -or
    $currentTasksHash -cnotmatch '^sha256:[0-9a-f]{64}$' -or $currentTasksHash -cne $state.tasks_hash) {
    Output-Gate "blocked" "Current tasks are not validated or their digest does not match validation" @("Run /speckit.spec-validate.validate-tasks with current tasks.md")
}

# Rule 4: Changes requested
if ($state.review_status -eq "changes-requested") {
    Output-Gate "blocked" "Reviewer requested changes" @("Address reviewer comments", "Re-run /speckit.spec-validate.validate")
}

# Peer review must independently identify the validated bytes.
if ($state.review_status -eq "approved" -and $state.reviewed_spec_hash -cne $currentHash) {
    Output-Gate "blocked" "Peer review does not match the current spec digest" @("Obtain peer review of current spec.md")
}

# Task review must be decided separately and bound to the current task bytes.
if ($state.tasks_review_status -isnot [string] -or
    $state.tasks_review_status -cnotin @('not-required', 'approved') -or
    ($state.tasks_review_status -ceq 'approved' -and
     ($state.reviewed_tasks_hash -isnot [string] -or $state.reviewed_tasks_hash -cne $currentTasksHash))) {
    Output-Gate "blocked" "Task review is unresolved or does not match the current tasks digest" @("Complete the task review requirement for current tasks.md")
}

# The shared policy only authorizes waiting spec review, never task review.
if ($state.spec_self_validation -is [string] -and $state.spec_self_validation -ceq 'passed' -and
    $state.review_status -is [string] -and $state.review_status -cin @('pending', 'timed-out')) {
    $python = Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $python) {
        $python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if ($python) {
        $policy = Join-Path $ScriptDir '../check-maintainer-override.py'
        $stateJson | & $python.Source $policy
        if ($LASTEXITCODE -eq 0) {
            Output-Gate "allowed-with-warning" "Recorded maintainer override permits implementation" @() @(
                "Unresolved spec review overridden by recorded maintainer authorization"
            )
        }
    }
}

# Rule 10: Validated + review met
if ($state.spec_self_validation -is [string] -and $state.spec_self_validation -ceq 'passed' -and
    $state.review_status -is [string] -and $state.review_status -cin @('not-required', 'approved')) {
    Output-Gate "allowed" "Spec and tasks validated and review requirements met" @()
}

# Fallback
Output-Gate "blocked" "Gate check did not match any allow condition" @("Run /speckit.spec-validate.status")
