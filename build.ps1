# Build Lazarus (PyInstaller onedir: dist\Lazarus\Lazarus.exe) and its Windows installer.
#   .\build.ps1                    build only (dist\ and build\ are gitignored)
#   .\build.ps1 -SelfTest <pdf>    build, then run the frozen exe's --selftest on a test PDF you pass
#   .\build.ps1 -Setup             build, then package dist\Lazarus as Lazarus-Setup-<version>.exe (+ .sha256)
#                                  with Inno Setup 6 (ISCC; a build tool only) into -SetupDir (outside the repo)
#   .\build.ps1 -Setup -NoBuild    package the existing dist\Lazarus without rebuilding
#   .\build.ps1 -Setup -TestBuild  a TEST installer ("Lazarus Test": its own AppId, folder and shortcut names),
#                                  so tests never touch a real installation
#   .\build.ps1 -Release -Version x.y.z
#                                  publish: refuses unless this Lazarus folder is clean in git, x.y.z is
#                                  lazarus.__version__, installer\release-notes\vx.y.z.md exists, the tag isn't
#                                  on GitHub yet and tools\privacy_check.py finds nothing; then
#                                    1. builds the Setup + .sha256 (+ the same installer as Lazarus-Setup.exe,
#                                       the constant name the README's download button points at),
#                                    2. syncs the PUBLIC source repo Crazy8697/Lazarus: a clone in -PublicDir
#                                       gets exactly this folder's committed files (git archive of HEAD, so
#                                       nothing outside this folder and none of its private history), the
#                                       README's version / size line, one commit "Lazarus x.y.z", pushed,
#                                    3. creates release vx.y.z there (gh CLI) on that commit with
#                                       Lazarus-Setup-x.y.z.exe, its .sha256 (the updater's names) and
#                                       Lazarus-Setup.exe.
#                                  The private working copy stays where it is (Forgejo); only this folder goes
#                                  to GitHub, and only on a release.
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
    [string]$SetupDir = (Join-Path $env:USERPROFILE "Lazarus-Setup"),
    [string]$PublicDir = (Join-Path $env:LOCALAPPDATA "Lazarus-public\Lazarus"),
    [switch]$FreshPublicHistory     # -Release only, once, deliberately: the public repo restarts from this release's
                                    # single commit (force-pushed), dropping whatever history it had
)
$ErrorActionPreference = "Stop"
# Python: $env:LAZARUS_PY, else .venv next to this script, else python on PATH
$py = if ($env:LAZARUS_PY) { $env:LAZARUS_PY }
      elseif (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")) { Join-Path $PSScriptRoot ".venv\Scripts\python.exe" }
      else { "python" }
$ReleasesRepo = "Crazy8697/Lazarus"
Push-Location $PSScriptRoot
try {
    $ver = (Select-String -Path (Join-Path $PSScriptRoot "lazarus\__init__.py") -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
    if ($Release) {
        if ($TestBuild) { throw "-Release never publishes a test build." }
        if ($Version -ne $ver) { throw "-Version '$Version' doesn't match lazarus.__version__ ($ver)." }
        $dirty = git -C $PSScriptRoot status --porcelain -- .
        if ($LASTEXITCODE -ne 0) { throw "git status failed" }
        if ($dirty) { throw "This Lazarus folder isn't clean in git; commit (to Forgejo) first." }
        & $py (Join-Path $PSScriptRoot "tools\privacy_check.py")
        if ($LASTEXITCODE -ne 0) { throw "The privacy check found something; nothing was published." }
        $notes = Join-Path $PSScriptRoot "installer\release-notes\v$ver.md"
        if (-not (Test-Path $notes)) { throw "No release notes: $notes" }
        # (through cmd: PowerShell 5.1 turns a redirected native stderr into a terminating error under "Stop")
        cmd /c "gh release view v$ver --repo $ReleasesRepo >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { throw "Release v$ver already exists on $ReleasesRepo." }
        $Setup = $true
    }

    $dist = Join-Path $PSScriptRoot "dist\Lazarus"
    if (-not $NoBuild) {
        & $py -m PyInstaller --noconfirm --clean lazarus.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    }
    $mb = [math]::Round((Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "Built: $dist\Lazarus.exe  ($mb MB)"

    if ($SelfTest) {
        $work = Join-Path $env:TEMP "lazarus-selftest"
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
        $report = Join-Path $work "report.json"
        New-Item -ItemType Directory -Force $work | Out-Null
        $p = Start-Process "$dist\Lazarus.exe" -ArgumentList "--selftest", "`"$report`"", "`"$SelfTest`"", "`"$work`"" -Wait -PassThru
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
        & $iscc /Q @defs (Join-Path $PSScriptRoot "installer\lazarus.iss")
        if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
        $base = if ($TestBuild) { "Lazarus-Test-Setup-$ver.exe" } else { "Lazarus-Setup-$ver.exe" }
        $out = Join-Path $SetupDir $base
        $hash = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
        Set-Content -Path "$out.sha256" -Value "$hash *$base" -Encoding ascii -NoNewline
        $smb = [math]::Round((Get-Item $out).Length / 1MB, 1)
        Write-Host "Setup: $out  ($smb MB, version $ver)"
        Write-Host "SHA-256: $hash"

        if ($Release) {
            # the constant-named copy for the README's one-click download (releases/latest/download/...)
            $plain = Join-Path $SetupDir "Lazarus-Setup.exe"
            Copy-Item $out $plain -Force

            # 1. the public source: exactly this folder's committed files, one commit per release
            if (-not (Test-Path (Join-Path $PublicDir ".git"))) {
                New-Item -ItemType Directory -Force (Split-Path $PublicDir) | Out-Null
                gh repo clone $ReleasesRepo $PublicDir
                if ($LASTEXITCODE -ne 0) { throw "Couldn't clone $ReleasesRepo" }
            }
            git -C $PublicDir pull --ff-only
            if ($LASTEXITCODE -ne 0) { throw "Couldn't update the public clone" }
            if ($FreshPublicHistory) {
                git -C $PublicDir checkout -q --orphan lazarus-fresh
                if ($LASTEXITCODE -ne 0) { throw "Couldn't start a fresh history" }
            }
            $prefix = (git -C $PSScriptRoot rev-parse --show-prefix).Trim().TrimEnd("/")
            $tar = Join-Path $env:TEMP "lazarus-public-$ver.tar"
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
                commit -q -m "Lazarus $ver"
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
            gh release create "v$ver" $out "$out.sha256" $plain --repo $ReleasesRepo --target $commit `
                --title "Lazarus $ver" --notes-file $notes
            if ($LASTEXITCODE -ne 0) { throw "gh release create failed ($LASTEXITCODE)" }
            Write-Host "Released v$ver on $ReleasesRepo (source commit $commit)"
        }
    }
} finally { Pop-Location }
