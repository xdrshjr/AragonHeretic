[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$Finalize,
    [switch]$Clean,
    [string]$PythonExe = 'python',
    [string]$TexBin = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Reject conflicting options before creating directories or touching manifests.
if (($CheckOnly -and $Finalize) -or
    ($Clean -and ($CheckOnly -or $Finalize))) {
    Write-Error 'CheckOnly/Finalize are exclusive; Clean requires normal build.'
    exit 2
}

$PaperDir = [IO.Path]::GetFullPath($PSScriptRoot).Replace('\', '/')
$RepoRoot = [IO.Path]::GetFullPath("$PaperDir/../../..").Replace('\', '/')
$BuildDir = "$PaperDir/build"
$ManifestPath = "$BuildDir/build-manifest.json"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$script:Commands = New-Object 'System.Collections.Generic.List[object]'
$script:Manifest = $null
$script:Step = 'initialization'
$script:Tools = $null
$script:EnvironmentBackup = @{}

function Set-LocalEnvironment([string]$Name, [string]$Value) {
    if (-not $script:EnvironmentBackup.ContainsKey($Name)) {
        $script:EnvironmentBackup[$Name] =
            [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Write-Json([string]$Path, $Value) {
    $serialized = ConvertTo-Json -InputObject $Value -Depth 100
    [IO.File]::WriteAllText("$Path.tmp", "$serialized`n", $Utf8)
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace("$Path.tmp", $Path,
            [System.Management.Automation.Language.NullString]::Value)
    } else {
        [IO.File]::Move("$Path.tmp", $Path)
    }
}

function Save-Manifest {
    if ($null -ne $script:Manifest) {
        $script:Manifest.commands = @($script:Commands.ToArray())
        Write-Json $ManifestPath $script:Manifest
    }
}

function ConvertTo-NativeArgument([string]$Value) {
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-LoggedCommand {
    param([string]$Name, [string]$Executable, [string[]]$Arguments,
          [int]$TimeoutSeconds = 180, [int[]]$AllowedExitCodes = @(0))
    $script:Step = $Name
    Write-Host "[$Name] $Executable"
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = ($Arguments | ForEach-Object {
        ConvertTo-NativeArgument $_
    }) -join ' '
    $start.WorkingDirectory = $PaperDir
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $Utf8
    $start.StandardErrorEncoding = $Utf8
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    $watch = [Diagnostics.Stopwatch]::StartNew()
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $finished = $process.WaitForExit($TimeoutSeconds * 1000)
    if (-not $finished) { $process.Kill(); $process.WaitForExit() }
    $output = $stdout.Result
    $errors = $stderr.Result
    $logPath = "$BuildDir/logs/$Name.log"
    [IO.File]::WriteAllText($logPath, "$output`n$errors", $Utf8)
    $script:Commands.Add(@{
        step = $Name; executable = $Executable; arguments = $Arguments
        exit_code = $process.ExitCode; timed_out = -not $finished
        duration_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
        log = "docs/papers/heretic-ara/build/logs/$Name.log"
    })
    Save-Manifest
    if (-not $finished) { throw "$Name timed out; see $logPath" }
    if ($process.ExitCode -notin $AllowedExitCodes) {
        throw "$Name exited $($process.ExitCode); see $logPath`n$errors"
    }
    return $output
}

function Invoke-PythonHelper([string]$Name, [string]$Expression) {
    $code = 'import sys; sys.dont_write_bytecode=True; ' +
        'sys.path.insert(0, sys.argv[1]); import verify_paper as v; ' +
        'import json; from pathlib import Path; ' + $Expression
    return Invoke-LoggedCommand $Name $PythonExe @(
        '-B', '-c', $code, "$PaperDir/evidence", $PaperDir, $TexBin
    )
}

function Initialize-Workspace {
    [void][IO.Directory]::CreateDirectory("$BuildDir/logs")
    [void][IO.Directory]::CreateDirectory("$BuildDir/tmp")
    Set-LocalEnvironment 'PYTHONDONTWRITEBYTECODE' '1'
    Set-LocalEnvironment 'PYTHONUTF8' '1'
    Set-LocalEnvironment 'TEMP' "$BuildDir/tmp"
    Set-LocalEnvironment 'TMP' "$BuildDir/tmp"
    $json = Invoke-PythonHelper 'workspace-environment' (
        'print(json.dumps(v.prepare_environment(Path(sys.argv[2]))))')
    $settings = $json | ConvertFrom-Json
    foreach ($property in $settings.PSObject.Properties) {
        Set-LocalEnvironment $property.Name ([string]$property.Value)
    }
}

function Initialize-Manifest {
    if ($CheckOnly) { return }
    if ($Finalize) {
        if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
            throw 'Finalize needs an existing pending_review build manifest.'
        }
        $script:Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath |
            ConvertFrom-Json
        if ($script:Manifest.status -notin @('pending_review', 'success')) {
            throw 'Previous build did not pass; run a normal build first.'
        }
        foreach ($command in $script:Manifest.commands) {
            $script:Commands.Add($command)
        }
        return
    }
    $script:Manifest = [ordered]@{
        status = 'building'
        built_at_utc = [DateTime]::UtcNow.ToString('o')
        checkout_head = $null
        commands = @(); tool_versions = @{}; input_hashes = @{}
        pdf_sha256 = $null; page_count = $null
        verification_report = 'docs/papers/heretic-ara/build/verification.json'
        review_sha256 = $null; failed_step = $null; error = $null
    }
    Save-Manifest
}

function Initialize-Tex {
    $json = Invoke-PythonHelper 'discover-tex' (
        'print(json.dumps(v.discover_tools(sys.argv[3] or None)))')
    $script:Tools = $json | ConvertFrom-Json
    Set-LocalEnvironment 'PAPER_TEXBIN' (
        [IO.Path]::GetDirectoryName($script:Tools.xelatex))
    $json = Invoke-PythonHelper 'tex-environment' (
        't=v.discover_tools(sys.argv[3] or None); ' +
        'print(json.dumps(v.prepare_environment(Path(sys.argv[2]),t)))')
    $settings = $json | ConvertFrom-Json
    foreach ($property in $settings.PSObject.Properties) {
        Set-LocalEnvironment $property.Name ([string]$property.Value)
    }
}

function Test-Dependencies {
    $versions = @{}
    $pythonCode = 'import sys; assert sys.version_info >= (3,11); ' +
        'print(sys.version)'
    $versions.python = (Invoke-LoggedCommand 'python-version' $PythonExe @(
        '-B', '-c', $pythonCode)).Trim()
    $matplotlibCode = 'import importlib.metadata as m; ' +
        'v=m.version("matplotlib"); assert v=="3.11.0", v; print(v)'
    $versions.matplotlib = (Invoke-LoggedCommand 'matplotlib-version' `
        $PythonExe @('-B', '-c', $matplotlibCode)).Trim()
    if (-not $versions.python -or -not $versions.matplotlib) {
        throw 'Python/matplotlib version output must not be empty.'
    }
    foreach ($name in @('xelatex', 'bibtex', 'pdftotext', 'pdftoppm')) {
        $switch = if ($name -like 'pdf*') { '-v' } else { '--version' }
        $allowed = @(0)
        if ($name -like 'pdf*' -and $script:Tools.pdf_family -eq 'xpdf') {
            $allowed += 99
        }
        $result = Invoke-LoggedCommand "$name-version" `
            $script:Tools.$name @($switch) -AllowedExitCodes $allowed
        $log = Get-Content -Raw -Encoding UTF8 "$BuildDir/logs/$name-version.log"
        $versions[$name] = ($log.Trim() -split "`n")[0].Trim()
    }
    $script:Manifest.tool_versions = $versions
    Save-Manifest
    foreach ($component in @('ctexart.cls', 'amsmath.sty', 'amssymb.sty',
        'booktabs.sty', 'tabularx.sty', 'graphicx.sty', 'hyperref.sty',
        'geometry.sty', 'setspace.sty', 'FandolSong-Regular.otf')) {
        $location = Invoke-LoggedCommand "find-$component" `
            $script:Tools.kpsewhich @($component)
        if (-not $location.Trim()) {
            throw "Missing TeX component: $component; no automatic installation."
        }
    }
}

function Remove-IntermediateFiles {
    $resolved = (Resolve-Path -LiteralPath $BuildDir).Path.Replace('\', '/')
    if (-not $resolved.StartsWith("$PaperDir/",
        [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe build directory: $resolved"
    }
    foreach ($name in @('paper.aux', 'paper.bbl', 'paper.blg', 'paper.log',
        'paper.out', 'paper.toc', 'paper.pdf', 'paper.txt', 'paper.xdv')) {
        $path = "$BuildDir/$name"
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Invoke-SourceChecks([bool]$Generate) {
    $arguments = @('-B', "$PaperDir/evidence/summarize_ara_v1.py",
        '--source', "$RepoRoot/docs/logs/ara-v1", '--paper-dir', $PaperDir)
    if (-not $Generate) { $arguments += '--check' }
    $null = Invoke-LoggedCommand 'data-artifacts' $PythonExe $arguments
    if ($Generate) {
        $null = Invoke-LoggedCommand 'numeric-tests' $PythonExe @(
            '-B', '-m', 'unittest', 'discover', '-s', "$PaperDir/evidence",
            '-p', 'test_summarize_ara_v1.py')
    }
    $null = Invoke-LoggedCommand 'source-verification' $PythonExe @(
        '-B', "$PaperDir/evidence/verify_paper.py", '--paper-dir', $PaperDir,
        '--check-sources-only')
}

function Invoke-Compilation {
    $arguments = @('-interaction=nonstopmode', '-halt-on-error',
        '-file-line-error', '-output-directory=build')
    $bibArguments = @('build/paper')
    if ($script:Manifest.tool_versions.xelatex -match 'MiKTeX') {
        $arguments += @('--disable-installer', '--disable-write18')
        $bibArguments = @('--disable-installer', 'build/paper')
    } else { $arguments += '-no-shell-escape' }
    $arguments += 'paper.tex'
    $null = Invoke-LoggedCommand 'xelatex-1' $script:Tools.xelatex $arguments
    $null = Invoke-LoggedCommand 'bibtex' $script:Tools.bibtex $bibArguments
    $null = Invoke-LoggedCommand 'xelatex-2' $script:Tools.xelatex $arguments
    $null = Invoke-LoggedCommand 'xelatex-3' $script:Tools.xelatex $arguments
    $log = Get-Content -Raw -Encoding UTF8 "$BuildDir/paper.log"
    $forbidden = 'Missing character:|undefined references|undefined citations|' +
        '(Citation|Reference).+undefined|LaTeX Error|Overfull \\[hv]box'
    if ($log -match $forbidden) {
        throw 'Final TeX log contains errors/undefined/overfull; see build/paper.log.'
    }
    $warnings = @($log -split "`n" | Where-Object { $_ -match 'Underfull' })
    $script:Manifest['underfull_warnings'] = $warnings
}

function Invoke-PdfChecks([bool]$RequireReview) {
    $pdf = if ($RequireReview) { "$PaperDir/paper.pdf" } else { "$BuildDir/paper.pdf" }
    if (-not $RequireReview) {
        $textArguments = @('-enc', 'UTF-8', '-layout', $pdf, "$BuildDir/paper.txt")
        if ($script:Tools.pdf_family -eq 'xpdf') {
            $textArguments = @('-cfg', "$BuildDir/xpdfrc") + $textArguments
        }
        $null = Invoke-LoggedCommand 'extract-text' `
            $script:Tools.pdftotext $textArguments
    }
    $arguments = @('-B', "$PaperDir/evidence/verify_paper.py", '--paper-dir',
        $PaperDir, '--pdf', $pdf, '--text', "$BuildDir/paper.txt")
    if ($RequireReview) { $arguments += '--require-review' }
    $null = Invoke-LoggedCommand 'pdf-verification' $PythonExe $arguments
    return Get-Content -Raw -Encoding UTF8 "$BuildDir/verification.json" |
        ConvertFrom-Json
}

function Render-AllPages([int]$PageCount) {
    [void][IO.Directory]::CreateDirectory("$BuildDir/pages")
    $resolved = (Resolve-Path -LiteralPath "$BuildDir/pages").Path.Replace('\', '/')
    if (-not $resolved.StartsWith("$PaperDir/",
        [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe pages directory.' }
    foreach ($page in Get-ChildItem -LiteralPath "$BuildDir/pages" -Filter 'page-*.png') {
        Remove-Item -LiteralPath $page.FullName -Force
    }
    $arguments = @('-r', '120', '-png', "$BuildDir/paper.pdf", "$BuildDir/pages/page")
    if ($script:Tools.pdf_family -eq 'xpdf') {
        $arguments = @('-cfg', "$BuildDir/xpdfrc", '-r', '120',
            "$BuildDir/paper.pdf", "$BuildDir/pages/page")
    }
    $null = Invoke-LoggedCommand 'render-all-pages' $script:Tools.pdftoppm $arguments
    if ($script:Tools.pdf_family -eq 'xpdf') {
        $null = Invoke-PythonHelper 'render-png' (
            'print(v.convert_rendered_pages(Path(sys.argv[2])))')
    }
    $pages = @(Get-ChildItem -LiteralPath "$BuildDir/pages" -Filter 'page-*.png' |
        ForEach-Object { [int]($_.BaseName -replace '^page-', '') } | Sort-Object)
    if (($pages -join ',') -ne ((1..$PageCount) -join ',')) {
        throw 'Rendered page set does not exactly match the current PDF.'
    }
}

function Complete-AutomaticBuild {
    $script:Manifest.checkout_head = (Invoke-PythonHelper 'checkout-head' (
        'import subprocess; r=subprocess.run(["git","rev-parse","HEAD"], ' +
        'cwd=v.REPO_ROOT,capture_output=True,check=True,timeout=30); ' +
        'print(r.stdout.decode("ascii").strip())')).Trim()
    $null = Invoke-PythonHelper 'check-ai-image' (
        'v.check_architecture(Path(sys.argv[2])); print("AI image verified")')
    Invoke-SourceChecks $true
    Initialize-Tex
    Test-Dependencies
    if ($Clean) { Remove-IntermediateFiles }
    $hashes = Invoke-PythonHelper 'freeze-inputs' (
        'print(json.dumps(v.snapshot_inputs(Path(sys.argv[2]))))')
    $script:Manifest.input_hashes = $hashes | ConvertFrom-Json
    Save-Manifest
    Invoke-Compilation
    $report = Invoke-PdfChecks $false
    Render-AllPages $report.page_count
    $null = Invoke-PythonHelper 'recheck-inputs' (
        'v.snapshot_inputs(Path(sys.argv[2]),True); print("Inputs unchanged")')
    $null = Invoke-PythonHelper 'publish-pdf' (
        'import shutil,os; p=Path(sys.argv[2]); ' +
        's=p/"build/paper-publish.pdf"; shutil.copyfile(p/"build/paper.pdf",s); ' +
        'os.replace(s,p/"paper.pdf"); print("PDF published for review")')
    $script:Manifest.pdf_sha256 = $report.pdf_sha256
    $script:Manifest.page_count = $report.page_count
    $script:Manifest.status = 'pending_review'
    Save-Manifest
    Write-Host 'Automatic build passed; pending full-page review (pending_review).'
}

function Complete-Finalization {
    Invoke-SourceChecks $false
    $null = Invoke-PythonHelper 'finalize-inputs' (
        'v.snapshot_inputs(Path(sys.argv[2]),True); print("Inputs unchanged")')
    Initialize-Tex
    $report = Invoke-PdfChecks $true
    if ($report.pdf_sha256 -ne $script:Manifest.pdf_sha256 -or
        $report.page_count -ne $script:Manifest.page_count) {
        throw 'Current PDF does not match the successful automatic build.'
    }
    $null = Invoke-PythonHelper 'finalize-recheck-inputs' (
        'v.snapshot_inputs(Path(sys.argv[2]),True); print("Inputs unchanged")')
    $script:Manifest.status = 'success'
    $script:Manifest.review_sha256 = $report.review_sha256
    $script:Manifest.failed_step = $null
    $script:Manifest.error = $null
    Save-Manifest
    Write-Host 'Finalization passed: current inputs, PDF, text and review match (success).'
}

try {
    [void][IO.Directory]::CreateDirectory($BuildDir)
    Initialize-Manifest
    Initialize-Workspace
    if ($CheckOnly) {
        Invoke-SourceChecks $false
        Write-Host 'Source and artifact checks passed; no PDF compiled.'
    } elseif ($Finalize) { Complete-Finalization }
    else { Complete-AutomaticBuild }
    exit 0
} catch {
    if ($null -ne $script:Manifest) {
        $script:Manifest.status = 'failed'
        $script:Manifest.failed_step = $script:Step
        $script:Manifest.error = $_.Exception.Message
        Save-Manifest
    }
    [Console]::Error.WriteLine("Build failed at $script:Step : $($_.Exception.Message)")
    exit 1
} finally {
    foreach ($name in $script:EnvironmentBackup.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name, $script:EnvironmentBackup[$name], 'Process')
    }
}
