# Build Lazarus PDF (PyInstaller onedir: dist\LazarusPDF\LazarusPDF.exe) and its Windows installer.
#   .\build.ps1                    build only (dist\ and build\ are gitignored)
#   .\build.ps1 -SelfTest <pdf>    build, then run the frozen exe's --selftest on a test PDF you pass
#   .\build.ps1 -Setup             build, then package dist\LazarusPDF as LazarusPDF-Setup-<version>.exe (+ .sha256)
#                                  with Inno Setup 6 (ISCC; a build tool only) into -SetupDir (outside the repo)
#   .\build.ps1 -Setup -NoBuild    package the existing dist\LazarusPDF without rebuilding
#   .\build.ps1 -Setup -TestBuild  a TEST installer ("Lazarus PDF Test": its own AppId, folder and shortcut names),
#                                  so tests never touch a real installation
#   .\build.ps1 -Release -Version x.y.z
#                                  publish: refuses unless this Lazarus PDF folder is clean in git, x.y.z is
#                                  lazaruspdf.__version__, installer\release-notes\vx.y.z.md exists, the tag isn't
#                                  on GitHub yet and tools\privacy_check.py finds nothing; then
#                                    1. builds the Setup + .sha256 (+ the same installer as LazarusPDF-Setup.exe,
#                                       the constant name the README's download button points at),
#                                    2. syncs the PUBLIC source repo Crazy8697/LazarusPDF: a clone in -PublicDir
#                                       gets exactly this folder's committed files (git archive of HEAD, so
#                                       nothing outside this folder and none of its private history), the
#                                       README's version / size line, one commit "Lazarus PDF x.y.z", pushed,
#                                    3. creates release vx.y.z there (gh CLI) on that commit with
#                                       LazarusPDF-Setup-x.y.z.exe, its .sha256 (the updater's names) and
#                                       LazarusPDF-Setup.exe; plus the same installer under the names Lazarus
#                                       0.21.0 and older look for (stage 22): Lazarus-Setup-x.y.z.exe + .sha256
#                                       (their updater) and Lazarus-Setup.exe (old download links).
#                                  The private working copy stays where it is (Forgejo); only this folder goes
#                                  to GitHub, and only on a release.
#                                    4. winget: updates DarkRelay.LazarusPDF in microsoft/winget-pkgs (a PR through
#                                       wingetcreate, versioned installer URL); never fatal. -NoWinget skips it,
#                                       -WingetDryRun builds and validates the manifests without submitting.
#   .\build.ps1 -WingetOnly -Version x.y.z [-WingetDryRun]
#                                  only step 4, for a release that is already published
# Installing is Setup's job: there is no copy-to-a-folder install any more.
# Needs a Python 3.11 venv with requirements.txt + requirements-build.txt installed (LAZARUS_PY = its
# python.exe if it isn't .venv here).
param(
    [string]$SelfTest = "",
    [switch]$Setup,
    [switch]$Release,
    [string]$Version = "",
    [switch]$NoBuild,
    [switch]$TestBuild,
    [string]$SetupDir = (Join-Path $env:USERPROFILE "LazarusPDF-Setup"),
    [string]$PublicDir = (Join-Path $env:LOCALAPPDATA "LazarusPDF-public\LazarusPDF"),
    [switch]$FreshPublicHistory,    # -Release only, once, deliberately: the public repo restarts from this release's
                                    # single commit (force-pushed), dropping whatever history it had
    [switch]$NoWinget,              # -Release: skip the winget update
    [switch]$WingetDryRun,          # -Release / -WingetOnly: build + validate the winget manifests, submit nothing
    [switch]$WingetOnly             # only the winget step, for an already published -Version
)
$ErrorActionPreference = "Stop"
# Python: $env:LAZARUS_PY, else .venv next to this script, else python on PATH
$py = if ($env:LAZARUS_PY) { $env:LAZARUS_PY }
      elseif (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")) { Join-Path $PSScriptRoot ".venv\Scripts\python.exe" }
      else { "python" }
$ReleasesRepo = "Crazy8697/LazarusPDF"

# ---- winget: DarkRelay.LazarusPDF in microsoft/winget-pkgs (never fatal: the GitHub release stands) ----
# Nothing here starts a program or opens a window: no installer is run, wingetcreate gets --no-open with
# --submit (the only time it would open a browser), and its GitHub token comes from WINGET_CREATE_GITHUB_TOKEN
# (gh auth token, at run time, never stored or printed) so it never asks for a login; without a token
# wingetcreate isn't run at all.
#   in winget already  -> wingetcreate update (versioned installer URL), manifests in -SetupDir\winget-<ver>,
#                         winget validate, then the PR (--submit) unless -WingetDryRun
#   not in winget yet  -> installer\winget\*.yaml filled in for this version and validated; the first
#                         submission is made by hand, so nothing is submitted
function Update-Winget([string]$ver, [switch]$DryRun) {
    $ErrorActionPreference = "Continue"
    $id = "DarkRelay.LazarusPDF"
    $asset = "LazarusPDF-Setup-$ver.exe"
    $url = "https://github.com/$ReleasesRepo/releases/download/v$ver/$asset"
    $outDir = Join-Path $SetupDir "winget-$ver"
    try {
        Remove-Item $outDir -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force $outDir | Out-Null
        # the release must be published, with the versioned installer and its published hash
        $rel = (cmd /c "gh api repos/$ReleasesRepo/releases/tags/v$ver 2>nul") -join "`n"
        if ($LASTEXITCODE -ne 0 -or -not $rel) { Write-Warning "winget: release v$ver isn't on $ReleasesRepo; skipped"; return }
        $relObj = $rel | ConvertFrom-Json
        $a = $relObj.assets | Where-Object { $_.name -eq $asset }
        if (-not $a -or -not $a.digest) { Write-Warning "winget: $asset (or its digest) isn't on the release; skipped"; return }
        $sha = ($a.digest -replace '^sha256:', '').ToUpper()
        $date = ([datetime]$relObj.published_at).ToString("yyyy-MM-dd")
        $winget = (Get-Command winget -ErrorAction SilentlyContinue).Source
        cmd /c "gh api repos/microsoft/winget-pkgs/contents/manifests/d/DarkRelay/LazarusPDF >nul 2>&1"
        $listed = $LASTEXITCODE -eq 0
        if (-not $listed) {
            $dir = Join-Path $outDir "manifests\d\DarkRelay\LazarusPDF\$ver"
            New-Item -ItemType Directory -Force $dir | Out-Null
            foreach ($f in Get-ChildItem (Join-Path $PSScriptRoot "installer\winget") -Filter "*.yaml") {
                $t = [IO.File]::ReadAllText($f.FullName).Replace("{{VERSION}}", $ver).Replace("{{SHA256}}", $sha).Replace("{{DATE}}", $date)
                [IO.File]::WriteAllText((Join-Path $dir $f.Name), $t, (New-Object Text.UTF8Encoding $false))
            }
            if ($winget) {
                $v = cmd /c "`"$winget`" validate --manifest `"$dir`" --disable-interactivity 2>&1"
                Write-Host ("winget: validate: " + (($v | Where-Object { $_ -match '\S' }) -join " "))
            }
            Write-Host "winget: $id isn't in microsoft/winget-pkgs yet. The first submission is made by hand; manifests for $ver are in $dir (nothing submitted)."
            return
        }
        $wc = (Get-Command wingetcreate -ErrorAction SilentlyContinue).Source
        if (-not $wc) { $wc = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\wingetcreate.exe" }
        if (-not (Test-Path $wc)) { Write-Warning "winget: wingetcreate isn't installed (winget install Microsoft.WingetCreate); skipped"; return }
        $tok = (cmd /c "gh auth token 2>nul") -join ""
        if ($LASTEXITCODE -ne 0 -or -not $tok.Trim()) { Write-Warning "winget: no GitHub token from gh auth token; skipped (wingetcreate would ask for a login)"; return }
        $wargs = @("update", $id, "--version", $ver, "--urls", $url, "--release-date", $date,
                   "--release-notes-url", "https://github.com/$ReleasesRepo/releases/tag/v$ver", "--out", $outDir)
        # (--no-open always goes with --submit: the PR page never opens in a browser)
        if (-not $DryRun) { $wargs += @("--submit", "--no-open", "--prtitle", "New version: $id version $ver") }
        $env:WINGET_CREATE_GITHUB_TOKEN = $tok.Trim()
        try {
            $o = & $wc @wargs 2>&1
            $code = $LASTEXITCODE
        } finally {
            Remove-Item Env:\WINGET_CREATE_GITHUB_TOKEN -ErrorAction SilentlyContinue
            $tok = $null
        }
        $o | Where-Object { $_ -match '\S' } | ForEach-Object { Write-Host "wingetcreate: $_" }
        if ($code -ne 0) { Write-Warning "winget: wingetcreate update failed ($code); the GitHub release stands, nothing rolled back"; return }
        $inst = Get-ChildItem $outDir -Recurse -Filter "$id.installer.yaml" | Select-Object -First 1
        if (-not $inst -or -not ((Get-Content $inst.FullName -Raw) -match [regex]::Escape($sha))) {
            Write-Warning "winget: the updated manifest's InstallerSha256 isn't the release's ($sha); check $outDir"
        }
        if ($winget -and $inst) {
            $v = cmd /c "`"$winget`" validate --manifest `"$($inst.DirectoryName)`" --disable-interactivity 2>&1"
            Write-Host ("winget: validate: " + (($v | Where-Object { $_ -match '\S' }) -join " "))
        }
        Write-Host ("winget: " + $(if ($DryRun) { "dry run, nothing submitted; manifests in $outDir" } else { "update for $ver submitted to microsoft/winget-pkgs" }))
    } catch {
        Write-Warning "winget: $($_.Exception.Message); the GitHub release stands, nothing rolled back"
    }
}
Push-Location $PSScriptRoot
try {
    $ver = (Select-String -Path (Join-Path $PSScriptRoot "lazaruspdf\__init__.py") -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
    if ($WingetOnly) {
        if (-not $Version) { throw "-WingetOnly needs -Version x.y.z (a published release)." }
        Update-Winget $Version -DryRun:$WingetDryRun
        return
    }
    if ($Release) {
        if ($TestBuild) { throw "-Release never publishes a test build." }
        if ($Version -ne $ver) { throw "-Version '$Version' doesn't match lazarus.__version__ ($ver)." }
        $dirty = git -C $PSScriptRoot status --porcelain -- .
        if ($LASTEXITCODE -ne 0) { throw "git status failed" }
        if ($dirty) { throw "This Lazarus PDF folder isn't clean in git; commit (to Forgejo) first." }
        & $py (Join-Path $PSScriptRoot "tools\privacy_check.py")
        if ($LASTEXITCODE -ne 0) { throw "The privacy check found something; nothing was published." }
        $notes = Join-Path $PSScriptRoot "installer\release-notes\v$ver.md"
        if (-not (Test-Path $notes)) { throw "No release notes: $notes" }
        # (through cmd: PowerShell 5.1 turns a redirected native stderr into a terminating error under "Stop")
        cmd /c "gh release view v$ver --repo $ReleasesRepo >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { throw "Release v$ver already exists on $ReleasesRepo." }
        $Setup = $true
    }

    $dist = Join-Path $PSScriptRoot "dist\LazarusPDF"
    if (-not $NoBuild) {
        & $py -m PyInstaller --noconfirm --clean lazaruspdf.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    }
    $mb = [math]::Round((Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "Built: $dist\LazarusPDF.exe  ($mb MB)"

    if ($SelfTest) {
        $work = Join-Path $env:TEMP "lazaruspdf-selftest"
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
        $report = Join-Path $work "report.json"
        New-Item -ItemType Directory -Force $work | Out-Null
        $p = Start-Process "$dist\LazarusPDF.exe" -ArgumentList "--selftest", "`"$report`"", "`"$SelfTest`"", "`"$work`"" -Wait -PassThru
        Write-Host "Self-test exit $($p.ExitCode); report: $report"
    }

    if ($Setup) {
        # Inno Setup's compiler: $env:LAZARUS_ISCC, else where its installer registered it
        $iscc = $env:LAZARUS_ISCC
        if (-not $iscc) {
            foreach ($k in "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                           "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1") {
                $loc = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallLocation
                if ($loc -and (Test-Path (Join-Path $loc "ISCC.exe"))) { $iscc = Join-Path $loc "ISCC.exe"; break }
            }
        }
        if (-not $iscc -or -not (Test-Path $iscc)) { throw "Inno Setup 6 (ISCC.exe) not found; set LAZARUS_ISCC" }
        New-Item -ItemType Directory -Force $SetupDir | Out-Null
        $defs = @("/DAppVersion=$ver", "/DDistDir=$dist", "/O$SetupDir")
        if ($TestBuild) { $defs += "/DTestBuild=1" }
        & $iscc /Q @defs (Join-Path $PSScriptRoot "installer\lazaruspdf.iss")
        if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
        $base = if ($TestBuild) { "LazarusPDF-Test-Setup-$ver.exe" } else { "LazarusPDF-Setup-$ver.exe" }
        $out = Join-Path $SetupDir $base
        $hash = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
        Set-Content -Path "$out.sha256" -Value "$hash *$base" -Encoding ascii -NoNewline
        $smb = [math]::Round((Get-Item $out).Length / 1MB, 1)
        Write-Host "Setup: $out  ($smb MB, version $ver)"
        Write-Host "SHA-256: $hash"

        if ($Release) {
            # the constant-named copy for the README's one-click download (releases/latest/download/...)
            $plain = Join-Path $SetupDir "LazarusPDF-Setup.exe"
            Copy-Item $out $plain -Force
            # the names Lazarus 0.21.0 and older look for (stage 22): their updater wants
            # Lazarus-Setup-<version>.exe + its .sha256; old links want Lazarus-Setup.exe. Same file.
            $legacyDir = Join-Path $SetupDir "legacy-$ver"
            New-Item -ItemType Directory -Force $legacyDir | Out-Null
            $legacy = Join-Path $legacyDir "Lazarus-Setup-$ver.exe"
            Copy-Item $out $legacy -Force
            Set-Content -Path "$legacy.sha256" -Value "$hash *Lazarus-Setup-$ver.exe" -Encoding ascii -NoNewline
            $legacyPlain = Join-Path $legacyDir "Lazarus-Setup.exe"
            Copy-Item $out $legacyPlain -Force

            # 1. the public source: exactly this folder's committed files, one commit per release
            if (-not (Test-Path (Join-Path $PublicDir ".git"))) {
                New-Item -ItemType Directory -Force (Split-Path $PublicDir) | Out-Null
                gh repo clone $ReleasesRepo $PublicDir
                if ($LASTEXITCODE -ne 0) { throw "Couldn't clone $ReleasesRepo" }
            }
            git -C $PublicDir pull --ff-only
            if ($LASTEXITCODE -ne 0) { throw "Couldn't update the public clone" }
            if ($FreshPublicHistory) {
                git -C $PublicDir checkout -q --orphan lazaruspdf-fresh
                if ($LASTEXITCODE -ne 0) { throw "Couldn't start a fresh history" }
            }
            $prefix = (git -C $PSScriptRoot rev-parse --show-prefix).Trim().TrimEnd("/")
            $tar = Join-Path $env:TEMP "lazaruspdf-public-$ver.tar"
            # (from the repo's top: run in this subfolder, git would apply the prefix twice: an empty archive)
            $top = (git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
            git -C $top archive --format=tar -o $tar "HEAD:$prefix"
            if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
            # -f: on a fresh (orphan) branch every file is still staged
            git -C $PublicDir rm -r -q -f --ignore-unmatch -- .
            if ($LASTEXITCODE -ne 0) { throw "Couldn't clear the public clone" }
            # Windows' own tar: a GNU tar earlier on PATH (Git's) reads "C:" as a remote host
            & (Join-Path $env:SystemRoot "System32\tar.exe") -xf $tar -C $PublicDir
            if ($LASTEXITCODE -ne 0) { throw "Couldn't unpack the source into the public clone" }
            Remove-Item $tar
            $nfiles = (Get-ChildItem $PublicDir -Recurse -File -Force | Where-Object { $_.FullName -notlike "*\.git\*" }).Count
            if ($nfiles -lt 20) { throw "Only $nfiles files in the public clone: refusing to publish" }
            $readme = Join-Path $PublicDir "README.md"
            $mbr = [math]::Round((Get-Item $out).Length / 1MB)
            $info = "<!-- dl-info -->`n<p align=""center""><sub>Windows 10 / 11, 64-bit &nbsp;&middot;&nbsp; version $ver &nbsp;&middot;&nbsp; " +
                    "$mbr MB &nbsp;&middot;&nbsp; <a href=""https://github.com/$ReleasesRepo/releases"">all releases</a></sub></p>`n<!-- /dl-info -->"
            $text = [IO.File]::ReadAllText($readme)
            $text = [regex]::Replace($text, "(?s)<!-- dl-info -->.*?<!-- /dl-info -->", $info)
            [IO.File]::WriteAllText($readme, $text, (New-Object Text.UTF8Encoding $false))
            git -C $PublicDir add -A
            # authored as the GitHub account's noreply address, never this machine's git identity
            $gu = gh api user --jq '.login + \" \" + (.id | tostring)'
            $login, $uid = $gu.Trim().Split(" ")
            git -C $PublicDir -c "user.name=$login" -c "user.email=$uid+$login@users.noreply.github.com" `
                commit -q -m "Lazarus PDF $ver"
            if ($LASTEXITCODE -ne 0) { throw "Nothing to commit in the public clone?" }
            if ($FreshPublicHistory) {
                git -C $PublicDir branch -D main
                git -C $PublicDir branch -m main
                git -C $PublicDir push --force origin main
            } else {
                git -C $PublicDir push origin HEAD
            }
            if ($LASTEXITCODE -ne 0) { throw "Couldn't push the public source" }
            $commit = (git -C $PublicDir rev-parse HEAD).Trim()

            # 2. the release, on that commit: the updater's assets + the constant-named installer
            gh release create "v$ver" $out "$out.sha256" $plain $legacy "$legacy.sha256" $legacyPlain `
                --repo $ReleasesRepo --target $commit --title "Lazarus PDF $ver" --notes-file $notes
            if ($LASTEXITCODE -ne 0) { throw "gh release create failed ($LASTEXITCODE)" }
            Write-Host "Released v$ver on $ReleasesRepo (source commit $commit)"

            # 3. winget (never fatal)
            if ($NoWinget) { Write-Host "winget: skipped (-NoWinget)" } else { Update-Winget $ver -DryRun:$WingetDryRun }
        }
    }
} finally { Pop-Location }
