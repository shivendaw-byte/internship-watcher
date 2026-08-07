# Guided setup for the internship watcher.
#
# Run this once. It asks a few questions and does everything else itself:
# signs you into GitHub, creates the repo, stores your email settings as
# encrypted GitHub secrets, sends you the current backlog, and switches on the
# automatic schedule.
#
# Safe to re-run as many times as you like. It skips anything already done.

# NOTE on error handling: this script must NOT use $ErrorActionPreference =
# "Stop". In Windows PowerShell 5.1, redirecting a native program's stderr
# (e.g. `gh auth status 2>&1`) wraps each line in a NativeCommandError, which
# under "Stop" aborts the whole script -- so gh merely *reporting* "you are not
# logged in" would kill setup before it could log you in. Instead we check
# $LASTEXITCODE explicitly after every external command, and run silent probes
# through cmd.exe so their stderr never reaches PowerShell at all.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Say  ($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "   [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "   [!]  $m" -ForegroundColor Yellow }
function Die  ($m) {
    Write-Host ""
    Write-Host "STOPPED: $m" -ForegroundColor Red
    Write-Host "Nothing is broken - just fix the above and run this again." -ForegroundColor DarkGray
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

# Run a command quietly via cmd.exe and return its exit code. Keeps native
# stderr out of PowerShell entirely, so no scary red text and no crash.
function Quiet ($cmdline) {
    cmd /c "$cmdline >nul 2>nul"
    return $LASTEXITCODE
}

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
if (-not $py) { Die "Python not found. Install from https://python.org (tick 'Add Python to PATH'), then re-run." }
Ok "Python found"

Say "Installing Python packages (quick)"
& $py -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "Could not install Python packages." }
Ok "Packages ready"

# ------------------------------------------------------------ 2. github auth
Say "Step 2 of 7: signing in to GitHub"

if ((Quiet "`"$gh`" auth status") -ne 0) {
    Write-Host ""
    Write-Host "   You are not signed in yet, so a browser window will open." -ForegroundColor DarkGray
    Write-Host "   Answer the prompts like this:" -ForegroundColor DarkGray
    Write-Host "     Where do you use GitHub?      ->  GitHub.com" -ForegroundColor DarkGray
    Write-Host "     Preferred protocol?           ->  HTTPS" -ForegroundColor DarkGray
    Write-Host "     Authenticate Git?             ->  Y" -ForegroundColor DarkGray
    Write-Host "     How to authenticate?          ->  Login with a web browser" -ForegroundColor DarkGray
    Write-Host "   Then copy the one-time code it shows and paste it in the browser." -ForegroundColor DarkGray
    Write-Host ""

    & $gh auth login

    if ((Quiet "`"$gh`" auth status") -ne 0) {
        Die "GitHub sign-in did not complete. Run this script again to retry."
    }
}

$who = (cmd /c "`"$gh`" api user --jq .login 2>nul").Trim()
if (-not $who) { Die "Signed in, but could not read your GitHub username." }
Ok "Signed in as $who"

# ------------------------------------------------------------- 3. email info
Say "Step 3 of 7: your email settings"

if (Test-Path ".env") {
    Warn ".env already exists - keeping it. Delete that file and re-run to change it."
} else {
    Write-Host ""
    Write-Host "   The watcher sends mail through a Gmail account." -ForegroundColor DarkGray
    Write-Host "   You need an APP PASSWORD, not your normal Gmail password:" -ForegroundColor DarkGray
    Write-Host "     1. Turn on 2-Step Verification on that Google account" -ForegroundColor DarkGray
    Write-Host "     2. Go to  https://myaccount.google.com/apppasswords" -ForegroundColor DarkGray
    Write-Host "     3. Create one called 'internship watcher'" -ForegroundColor DarkGray
    Write-Host "     4. Copy the 16-character code it shows you" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "   Note: school/work Google accounts usually block app passwords." -ForegroundColor DarkGray
    Write-Host "   If step 2 shows nothing, use a personal Gmail to SEND," -ForegroundColor DarkGray
    Write-Host "   and still RECEIVE at your school address. That works fine." -ForegroundColor DarkGray
    Write-Host ""

    $sender = (Read-Host "   Gmail address that SENDS the alerts").Trim()
    if (-not $sender) { Die "No sender address given." }

    Write-Host "   (the password will stay invisible as you type - that is normal)" -ForegroundColor DarkGray
    $secure = Read-Host "   Its 16-character app password" -AsSecureString
    $bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $appPw  = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if (-not $appPw) { Die "No app password given." }
    $appPw = $appPw -replace '\s', ''

    $to = (Read-Host "   Where should alerts be DELIVERED? (your everyday inbox)").Trim()
    if (-not $to) { $to = $sender }

    try {
        @(
            "SMTP_HOST=smtp.gmail.com"
            "SMTP_PORT=587"
            "SMTP_USER=$sender"
            "SMTP_PASS=$appPw"
            "EMAIL_FROM=$sender"
            "EMAIL_TO=$to"
        ) | Set-Content -Path ".env" -Encoding utf8 -ErrorAction Stop
    } catch {
        Die "Could not write the .env file: $_"
    }
    Ok "Saved to .env (this file is never uploaded to GitHub)"
}

Say "Sending a test email"
& $py watcher.py --test-email
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Warn "The test email failed - almost always a wrong app password."
    Warn "Delete the file .env in this folder, then run this script again."
    Die "Email must work before the rest is worth doing."
}
Ok "Test sent - check that inbox before continuing"

# ---------------------------------------------------------------- 4. the repo
Say "Step 4 of 7: creating the public GitHub repo"

# Public deliberately: public repos get UNLIMITED free Actions minutes, which
# is what makes 30-minute polling free. No credentials live in the repo --
# .env is gitignored and the real values go to encrypted GitHub secrets.
$repoName = "internship-watcher"

if ((Quiet "`"$gh`" repo view $who/$repoName --json name") -eq 0) {
    Warn "Repo $who/$repoName already exists - reusing it"
    Quiet "git remote remove origin" | Out-Null
    & git remote add origin "https://github.com/$who/$repoName.git"
    & git push -u origin main
    if ($LASTEXITCODE -ne 0) { Die "Could not push to the existing repo." }
} else {
    & $gh repo create $repoName --public --source . --push
    if ($LASTEXITCODE -ne 0) { Die "Could not create the repo." }
}
Ok "Code is on GitHub (public)"

# ------------------------------------------------------------- 5. the secrets
Say "Step 5 of 7: storing your email settings as encrypted GitHub secrets"

$envMap = @{}
foreach ($line in (Get-Content ".env")) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $envMap[$matches[1]] = $matches[2] }
}
foreach ($k in @("SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","EMAIL_FROM","EMAIL_TO")) {
    if (-not $envMap.ContainsKey($k)) { Die "$k is missing from .env" }
    $envMap[$k] | & $gh secret set $k --repo "$who/$repoName"
    if ($LASTEXITCODE -ne 0) { Die "Could not store secret $k" }
}
Ok "All 6 secrets stored (GitHub encrypts these; they cannot be read back)"

# ------------------------------------------------------------- 6. the backlog
Say "Step 6 of 7: emailing you everything that is open RIGHT NOW"

if (Test-Path "state.json") {
    Warn "Already seeded - skipping the backlog email"
} else {
    Write-Host "   This takes about 2 minutes. Please wait..." -ForegroundColor DarkGray
    & $py watcher.py --notify-first-run
    if ($LASTEXITCODE -ge 2) { Die "The run failed before sending. See the error above." }
    Ok "Backlog email sent"
}

if (Test-Path "state.json") {
    & git add state.json
    Quiet "git commit -m `"Seed initial state`"" | Out-Null
    Quiet "git push origin main" | Out-Null
    Ok "Saved what it has already seen, so it will not re-alert you"
}

# ------------------------------------------------------------ 7. switching on
Say "Step 7 of 7: switching on the automatic schedule"

if ((Quiet "`"$gh`" workflow run internship-watch --repo $who/$repoName") -ne 0) {
    Warn "Could not start a test run automatically."
    Warn "Open https://github.com/$who/$repoName/actions and click 'Run workflow'."
} else {
    Ok "Test run started"
}

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green
Write-Host "  DONE. It now runs by itself." -ForegroundColor Green
Write-Host "  ------------------------------------------------------------" -ForegroundColor Green
Write-Host ""
Write-Host "  Aug-Jan (recruiting season):  every 30 min, 8am-7pm ET weekdays"
Write-Host "                                every 3 hours otherwise"
Write-Host "  Feb-Jul (quiet):              3 times a day"
Write-Host ""
Write-Host "  You will get an email when something new opens."
Write-Host "  If a site breaks, you get told - silence always means"
Write-Host "  nothing new, never a broken watcher."
Write-Host ""
Write-Host "  Check on it:  https://github.com/$who/$repoName/actions"
Write-Host ""
Read-Host "Press Enter to close"
