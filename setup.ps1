# Guided setup for the internship watcher.
#
# Run this once. It asks a few questions and does everything else itself:
# signs you into GitHub, creates the repo, stores your email settings as
# encrypted GitHub secrets, sends you the current backlog, and switches on the
# automatic schedule.
#
# Safe to re-run. It skips anything already done.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Say  ($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "   [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "   [!]  $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host ""; Write-Host "STOPPED: $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  Internship Watcher setup" -ForegroundColor White
Write-Host "  Answer the questions. Press Ctrl+C any time to bail out." -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1. tooling
Say "Step 1 of 7: checking tools"

$gh = $null
foreach ($p in @("$env:ProgramFiles\GitHub CLI\gh.exe",
                 "${env:ProgramFiles(x86)}\GitHub CLI\gh.exe",
                 "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe")) {
    if (Test-Path $p) { $gh = $p; break }
}
if (-not $gh) {
    $c = Get-Command gh -ErrorAction SilentlyContinue
    if ($c) { $gh = $c.Source }
}
if (-not $gh) { Die "GitHub CLI not found. Install it with:  winget install --id GitHub.cli" }
Ok "GitHub CLI found"

$py = $null
foreach ($n in @("python", "py")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $py = $c.Source; break }
}
if (-not $py) { Die "Python not found. Install it from https://python.org and re-run." }
Ok "Python found"

Say "Installing Python packages (quick)"
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "Could not install Python packages." }
Ok "Packages ready"

# ------------------------------------------------------------ 2. github auth
Say "Step 2 of 7: signing in to GitHub"

& $gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   A browser window will open. Sign in, then come back here." -ForegroundColor DarkGray
    Write-Host "   (Choose: GitHub.com  ->  HTTPS  ->  Login with a web browser)" -ForegroundColor DarkGray
    Write-Host ""
    & $gh auth login
    if ($LASTEXITCODE -ne 0) { Die "GitHub sign-in did not complete." }
}
$who = (& $gh api user --jq .login 2>$null)
Ok "Signed in as $who"

# ------------------------------------------------------------- 3. email info
Say "Step 3 of 7: your email settings"

if (Test-Path ".env") {
    Warn ".env already exists - keeping it. Delete it and re-run to change."
} else {
    Write-Host ""
    Write-Host "   The watcher sends mail through a Gmail account." -ForegroundColor DarkGray
    Write-Host "   You need an APP PASSWORD, not your normal Gmail password:" -ForegroundColor DarkGray
    Write-Host "     1. Turn on 2-Step Verification on that Google account" -ForegroundColor DarkGray
    Write-Host "     2. Go to  https://myaccount.google.com/apppasswords" -ForegroundColor DarkGray
    Write-Host "     3. Create one called 'internship watcher'" -ForegroundColor DarkGray
    Write-Host "     4. Copy the 16-character code it shows you" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "   Note: your Penn account probably blocks app passwords." -ForegroundColor DarkGray
    Write-Host "   If step 2 shows nothing, use a personal Gmail to SEND," -ForegroundColor DarkGray
    Write-Host "   and still RECEIVE at your Penn address. That works fine." -ForegroundColor DarkGray
    Write-Host ""

    $sender = Read-Host "   Gmail address that SENDS the alerts"
    if (-not $sender) { Die "No sender address given." }

    $secure = Read-Host "   Its 16-character app password (hidden as you type)" -AsSecureString
    $bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $appPw  = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if (-not $appPw) { Die "No app password given." }
    $appPw = $appPw -replace '\s', ''

    $to = Read-Host "   Where should alerts be DELIVERED? [sdawda@sas.upenn.edu]"
    if (-not $to) { $to = "sdawda@sas.upenn.edu" }

    @(
        "SMTP_HOST=smtp.gmail.com"
        "SMTP_PORT=587"
        "SMTP_USER=$sender"
        "SMTP_PASS=$appPw"
        "EMAIL_FROM=$sender"
        "EMAIL_TO=$to"
    ) | Set-Content -Path ".env" -Encoding utf8
    Ok "Saved to .env (this file is never uploaded to GitHub)"
}

Say "Sending a test email"
& $py watcher.py --test-email
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Warn "The test email failed - almost always a wrong app password."
    Warn "Delete .env and re-run this script to try again."
    Die "Fix the email settings first; everything else depends on them."
}
Ok "Test sent - check your inbox before continuing"

# ---------------------------------------------------------------- 4. the repo
Say "Step 4 of 7: creating the private GitHub repo"

$repoName = "internship-watcher"
$existing = (& $gh repo view "$who/$repoName" --json name 2>$null)
if ($LASTEXITCODE -eq 0 -and $existing) {
    Warn "Repo $who/$repoName already exists - reusing it"
    & git remote remove origin 2>$null | Out-Null
    & git remote add origin "https://github.com/$who/$repoName.git"
    & git push -u origin main
} else {
    & $gh repo create $repoName --private --source . --push
    if ($LASTEXITCODE -ne 0) { Die "Could not create the repo." }
}
Ok "Code is on GitHub (private)"

# ------------------------------------------------------------- 5. the secrets
Say "Step 5 of 7: storing your email settings as encrypted GitHub secrets"

$envMap = @{}
foreach ($line in (Get-Content ".env")) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $envMap[$matches[1]] = $matches[2] }
}
foreach ($k in @("SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","EMAIL_FROM","EMAIL_TO")) {
    if (-not $envMap.ContainsKey($k)) { Die "$k missing from .env" }
    $envMap[$k] | & $gh secret set $k --repo "$who/$repoName"
    if ($LASTEXITCODE -ne 0) { Die "Could not store secret $k" }
}
Ok "All 6 secrets stored (GitHub encrypts these; nobody can read them back)"

# ------------------------------------------------------------- 6. the backlog
Say "Step 6 of 7: emailing you everything that is open RIGHT NOW"

if (Test-Path "state.json") {
    Warn "Already seeded - skipping the backlog email"
} else {
    Write-Host "   This takes about 2 minutes and sends one big catalogue email." -ForegroundColor DarkGray
    & $py watcher.py --notify-first-run
    if ($LASTEXITCODE -ge 2) { Die "The run failed before sending. See the error above." }
    Ok "Backlog email sent"
}

if (Test-Path "state.json") {
    & git add state.json
    & git commit -q -m "Seed initial state" 2>$null | Out-Null
    & git push -q origin main 2>$null | Out-Null
    Ok "Saved what it has already seen, so it won't re-alert you"
}

# ------------------------------------------------------------ 7. switching on
Say "Step 7 of 7: switching on the automatic schedule"

& $gh workflow run internship-watch --repo "$who/$repoName" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Warn "Could not trigger a test run automatically."
    Warn "Open https://github.com/$who/$repoName/actions and click 'Run workflow'."
} else {
    Ok "Test run started"
}

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green
Write-Host "  DONE. It now runs by itself." -ForegroundColor Green
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green
Write-Host ""
Write-Host "  Aug-Jan (recruiting season):  every hour, 8am-7pm ET weekdays"
Write-Host "                                every 3 hours otherwise"
Write-Host "  Feb-Jul (quiet):              3 times a day"
Write-Host ""
Write-Host "  You will get an email when something new opens."
Write-Host "  If a site breaks, you get told - silence always means"
Write-Host "  nothing new, never a broken watcher."
Write-Host ""
Write-Host "  Check on it:  https://github.com/$who/$repoName/actions"
Write-Host ""
