<#
.SYNOPSIS
Build the paper and collect PDF checks for a subsequent manual review.
.PARAMETER Clean
Remove allowlisted auxiliary files after the evidence preflight succeeds.
.PARAMETER Open
Open the new PDF after machine checks complete.
.PARAMETER ToolchainDirectory
Prefer this directory; it must contain all three core TeX executables.
.NOTES
Exit 0 means machine checks completed, never final manuscript acceptance.
The report remains pending until the bound PDF is reviewed page by page.
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Open,
    [string]$ToolchainDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$reportRelativePath = 'paper-output/revision-check/build-report.json'
$originalPath = $env:PATH
$buildStartedUtc = [DateTime]::UtcNow
$utf8 = New-Object Text.UTF8Encoding($false)
$toolDiagnostics = New-Object 'Collections.Generic.List[object]'
$compilerRuns = New-Object 'Collections.Generic.List[object]'
$buildReport = $null

function ConvertTo-NativeArgument {
    param([string]$Value)
    # ProcessStartInfo on Windows PowerShell uses CRT command-line quoting.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-NativeCommand {
    param([string]$Executable, [string[]]$Arguments,
        [int]$TimeoutSeconds = 180)
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = ($Arguments | ForEach-Object {
        ConvertTo-NativeArgument $_
    }) -join ' '
    $start.WorkingDirectory = $projectRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $utf8
    $start.StandardErrorEncoding = $utf8
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while (-not $process.WaitForExit(10000)) {
            Write-Host ('      {0}: running ({1:N0}s)' -f
                [IO.Path]::GetFileName($Executable),
                $timer.Elapsed.TotalSeconds)
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $process.Kill()
                throw ('Command timed out: {0} {1}' -f
                    $Executable, $start.Arguments)
            }
        }
        $result = [ordered]@{
            executable = $Executable; arguments = @($Arguments)
            exit_code = $process.ExitCode
            stdout = $stdout.Result; stderr = $stderr.Result
        }
        if (-not [string]::IsNullOrWhiteSpace($result.stderr)) {
            $toolDiagnostics.Add([ordered]@{
                executable = $Executable; arguments = @($Arguments)
                stderr = $result.stderr.Trim(); disposition = 'pending'
            })
        }
        return $result
    }
    finally { $process.Dispose() }
}

function Assert-CommandSucceeded {
    param($Result)
    if ($Result.exit_code -ne 0) {
        throw ('{0} exited {1}. {2} {3}' -f $Result.executable,
            $Result.exit_code, $Result.stdout.Trim(), $Result.stderr.Trim())
    }
}

function Find-Application {
    param([string]$Name, [string]$Directory)
    if ($Directory) {
        $candidate = Join-Path $Directory "$Name.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate).Replace('\', '/')
        }
        return $null
    }
    $command = Get-Command $Name -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Path.Replace('\', '/') }
    return $null
}

function Find-CoreToolchain {
    $candidates = @()
    if ($ToolchainDirectory) {
        if (-not (Test-Path -LiteralPath $ToolchainDirectory `
                    -PathType Container)) {
            throw "Toolchain directory not found: $ToolchainDirectory"
        }
        $candidates = @((Resolve-Path -LiteralPath $ToolchainDirectory).Path)
    }
    else {
        foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
            if ($root) {
                $suffix = 'MiKTeX/miktex/bin/x64'
                if ($root -eq $env:LOCALAPPDATA) {
                    $suffix = "Programs/$suffix"
                }
                $candidates += Join-Path $root $suffix
            }
        }
        if (Test-Path -LiteralPath 'C:/texlive' -PathType Container) {
            $candidates += Get-ChildItem 'C:/texlive' -Directory |
                Sort-Object Name -Descending | ForEach-Object {
                    Join-Path $_.FullName 'bin/windows'
                }
        }
        $pathEngine = Find-Application 'xelatex'
        if ($pathEngine) { $candidates += Split-Path $pathEngine -Parent }
    }
    foreach ($directory in ($candidates | Select-Object -Unique)) {
        $missing = @('xelatex', 'bibtex', 'kpsewhich') | Where-Object {
            -not (Find-Application $_ $directory)
        }
        if (@($missing).Count -eq 0) {
            return [IO.Path]::GetFullPath($directory).Replace('\', '/')
        }
        if ($ToolchainDirectory) {
            throw "Missing core tools in ${directory}: $($missing -join ', ')"
        }
    }
    throw ('No complete TeX toolchain: ' +
        'xelatex, bibtex and kpsewhich are required.')
}

function Find-Python {
    foreach ($name in @('python', 'python3', 'py')) {
        $candidate = Find-Application $name
        if (-not $candidate) { continue }
        $arguments = @('-c', 'import sys; print(sys.executable)')
        if ($name -eq 'py') { $arguments = @('-3') + $arguments }
        $probe = Invoke-NativeCommand $candidate $arguments 20
        if ($probe.exit_code -ne 0) { continue }
        $actual = $probe.stdout.Trim().Replace('\', '/')
        if (-not (Test-Path -LiteralPath $actual -PathType Leaf)) { continue }
        $code = 'import sys; print(sys.version.split()[0]); ' +
            'sys.exit(0 if sys.version_info >= (3,11) else 1)'
        $version = Invoke-NativeCommand $actual @('-c', $code) 20
        if ($version.exit_code -eq 0) {
            return [ordered]@{
                path = $actual; version = $version.stdout.Trim()
                version_output = $version.stdout.Trim()
            }
        }
    }
    throw 'Python 3.11 or later is required for the evidence helper.'
}

function Get-Toolchain {
    $directory = Find-CoreToolchain
    $env:PATH = "$directory;$originalPath"
    $resolved = [ordered]@{ python = (Find-Python) }
    $pdfTools = @('pdftotext', 'pdfinfo', 'pdffonts', 'pdftoppm')
    foreach ($name in (@('xelatex', 'bibtex', 'kpsewhich') + $pdfTools)) {
        $path = Find-Application $name $directory
        if (-not $path -and $name -in $pdfTools) {
            $path = Find-Application $name
        }
        if (-not $path) { throw "Required tool not found: $name" }
        $flag = '--version'
        if ($name -in $pdfTools) { $flag = '-v' }
        $probe = Invoke-NativeCommand $path @($flag) 20
        Assert-CommandSucceeded $probe
        $output = ($probe.stdout + "`n" + $probe.stderr).Trim()
        $version = @($output -split '\r?\n' | Where-Object {
            $_ -match '(?i)(MiKTeX|version|XeTeX|kpathsea)' -and
            $_ -notmatch '(?i)(security risk|major issue|updates)'
        } | Select-Object -First 1)
        if ($version.Count -ne 1) { throw "No version from $path" }
        $resolved[$name] = [ordered]@{
            path = $path; version = $version[0]; version_output = $output
        }
        Write-Host "      ${name}: $path [$($version[0])]"
    }
    $probe = Invoke-NativeCommand $resolved.kpsewhich.path `
        @('-no-mktex=tex', 'pgfplots.sty') 20
    Assert-CommandSucceeded $probe
    $package = $probe.stdout.Trim()
    if (-not $package -or
            -not (Test-Path -LiteralPath $package -PathType Leaf)) {
        throw 'Missing dependency: pgfplots.sty in the selected TeX toolchain.'
    }
    $resolved['pgfplots'] = [ordered]@{
        path = $package.Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash
    }
    return $resolved
}

function Get-PaperPath {
    param([string]$RelativePath)
    $path = [IO.Path]::GetFullPath((Join-Path $projectRoot $RelativePath))
    $prefix = $projectRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes the paper directory: $RelativePath"
    }
    $cursor = $path
    while ($cursor.Length -ge $projectRoot.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Output path contains a link: $cursor"
            }
        }
        $cursor = Split-Path -Path $cursor -Parent
    }
    return $path.Replace('\', '/')
}

function Remove-PaperFile {
    param([string]$RelativePath)
    $path = Get-PaperPath $RelativePath
    if (Test-Path -LiteralPath $path) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Expected an ordinary output file: $path"
        }
        Remove-Item -LiteralPath $path -Force
    }
}

function Get-FileRecord {
    param([string]$RelativePath)
    $path = Get-PaperPath $RelativePath
    $file = Get-Item -LiteralPath $path
    return [ordered]@{
        path = $RelativePath; bytes = $file.Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    }
}

function Get-SourceRecords {
    $paths = @('paper.tex', 'references.bib', 'build.ps1',
        'evidence/summarize_ara_v1.py', 'evidence/ara-v1-summary.json',
        'evidence/ara-v1-trials.csv')
    foreach ($directory in @('sections', 'tables', 'figures')) {
        $paths += Get-ChildItem (Get-PaperPath $directory) -Filter '*.tex' `
            -File -Recurse | ForEach-Object {
                $_.FullName.Substring($projectRoot.Length + 1).Replace('\', '/')
            }
    }
    return @($paths | Sort-Object -Unique | ForEach-Object {
        Get-FileRecord $_
    })
}

function Assert-SourcesUnchanged {
    param($Records)
    $before = ConvertTo-Json -InputObject @($Records) -Depth 6 -Compress
    $after = ConvertTo-Json -InputObject @(Get-SourceRecords) -Depth 6 -Compress
    if ($before -cne $after) {
        throw 'Paper sources changed during this build; rebuild before review.'
    }
}

function Write-BuildReport {
    param($Report)
    if ($Report.status -ne 'stale') { Assert-PdfIdentity $Report }
    $path = Get-PaperPath $reportRelativePath
    [void][IO.Directory]::CreateDirectory((Split-Path $path -Parent))
    $json = ConvertTo-Json -InputObject $Report -Depth 16
    [IO.File]::WriteAllText($path, $json + "`n", $utf8)
}

function Set-StaleReport {
    param([string]$Reason)
    $path = Get-PaperPath $reportRelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return }
    try {
        $previous = Get-Content -LiteralPath $path -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        $previous = [pscustomobject]@{ schema_version = 'ara-paper-build-v1' }
    }
    foreach ($entry in @{
        status = 'stale'; stage = 'failed'
        stale_reason = $Reason; stale_at_utc = [DateTime]::UtcNow.ToString('o')
    }.GetEnumerator()) {
        $previous | Add-Member NoteProperty $entry.Key $entry.Value -Force
    }
    Write-BuildReport $previous
}

function Invoke-EvidenceCheck {
    param($Toolchain)
    $helper = Get-PaperPath 'evidence/summarize_ara_v1.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw "Evidence helper not found: $helper"
    }
    $result = Invoke-NativeCommand $Toolchain.python.path `
        @('-B', $helper, '--check')
    Assert-CommandSucceeded $result
    Write-Host $result.stdout.Trim()
    return [ordered]@{
        status = 'passed'; exit_code = $result.exit_code
        output = $result.stdout.Trim()
    }
}

function Invoke-CompilerPass {
    param($Toolchain, [string]$Name, [string]$Stage)
    $arguments = @('paper')
    if ($Name -eq 'xelatex') {
        $arguments = @('-interaction=nonstopmode', '-halt-on-error',
            '-file-line-error', '-recorder', 'paper.tex')
    }
    if ($Toolchain[$Name].version -match 'MiKTeX') {
        $arguments = @('-disable-installer') + $arguments
    }
    Write-Host "      $Stage..." -ForegroundColor Cyan
    $started = [DateTime]::UtcNow
    $result = Invoke-NativeCommand $Toolchain[$Name].path $arguments 600
    $compilerRuns.Add([ordered]@{
        stage = $Stage; tool = $Name; exit_code = $result.exit_code
        started_at_utc = $started.ToString('o')
        finished_at_utc = [DateTime]::UtcNow.ToString('o')
    })
    Assert-CommandSucceeded $result
}

function Get-LogWarnings {
    param([string]$RelativePath)
    $path = Get-PaperPath $RelativePath
    $lines = [IO.File]::ReadAllLines($path, $utf8)
    $notice = 'LaTeX Warning: You have requested release ' +
        '`2026/06/01'' of LaTeX, but only release ' +
        '`2025-11-01'' is available.'
    $fatal = '(?i)(Overfull \\[hv]box|Missing character:|' +
        '(?:LaTeX|Package \S+|Class \S+) Error:|^!|' +
        'undefined (?:references|citations)|' +
        '(?:Citation|Reference).*undefined|' +
        'multiply[ -]defined|LaTeX Font Warning:|font.*substitut|' +
        'Undefined control sequence|' +
        'I couldn''t open (?:database|style) file)'
    for ($index = 0; $index -lt $lines.Length; $index++) {
        $line = $lines[$index]
        $kind = $null
        if ($line -match $fatal) { $kind = 'fatal' }
        elseif ($line -match '^Underfull \\[hv]box') { $kind = 'underfull' }
        elseif ($line -match '^(LaTeX|Package .+?|Class .+?) Warning:|' +
                '^Warning--|^pdfTeX warning') { $kind = 'unresolved' }
        if (-not $kind) { continue }
        $lineNumber = $index + 1
        $block = $line
        while ($index + 1 -lt $lines.Length -and
                $lines[$index + 1] -match '^(\s+\S|\([^)]*\)\s+\S)') {
            $index++
            $block += ' ' + $lines[$index].Trim()
        }
        $normalized = [regex]::Replace($block, '\s+', ' ').Trim()
        if ($normalized -match $fatal) { $kind = 'fatal' }
        $disposition = 'pending'
        if ($normalized -ceq $notice) {
            $kind = 'known_release_notice'
            $disposition = 'nonfatal_exact_release_notice'
        }
        if ($kind -eq 'fatal') { $disposition = 'rejected' }
        [ordered]@{
            log = $RelativePath; line = $lineNumber; text = $normalized
            kind = $kind; disposition = $disposition
        }
    }
}

function Test-CompilerLogs {
    param([DateTime]$CompileStarted)
    $records = @()
    $warnings = @()
    foreach ($name in @('paper.log', 'paper.blg')) {
        $path = Get-PaperPath $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing current build log: $path"
        }
        $file = Get-Item -LiteralPath $path
        if ($file.Length -eq 0 -or $file.LastWriteTimeUtc -lt $CompileStarted) {
            throw "Empty or stale build log: $path"
        }
        $records += Get-FileRecord $name
        $warnings += @(Get-LogWarnings $name)
    }
    $fatal = @($warnings | Where-Object { $_.kind -eq 'fatal' })
    if ($fatal.Count -gt 0) {
        $details = ($fatal | ForEach-Object {
            '{0}:{1}: {2}' -f $_.log, $_.line, $_.text
        }) -join "`n"
        throw "Fatal compiler diagnostics:`n$details"
    }
    return [ordered]@{
        status = 'passed'; logs = $records; warnings = @($warnings)
        known_release_notice_count = @($warnings | Where-Object {
            $_.kind -eq 'known_release_notice'
        }).Count
        warning_review_status = 'pending'
    }
}

function Invoke-Compilation {
    param($Toolchain)
    if ($Clean) {
        foreach ($extension in @('aux', 'bbl', 'blg', 'dvi', 'fdb_latexmk',
                'fls', 'log', 'out', 'run.xml', 'synctex.gz', 'xdv')) {
            Remove-PaperFile "paper.$extension"
        }
    }
    # These are removed even without -Clean, so old artifacts cannot pass.
    foreach ($name in @('paper.pdf', 'paper.log', 'paper.blg')) {
        Remove-PaperFile $name
    }
    $compileStarted = [DateTime]::UtcNow
    Invoke-CompilerPass $Toolchain 'xelatex' 'XeLaTeX pass 1/3'
    Invoke-CompilerPass $Toolchain 'bibtex' 'BibTeX'
    Invoke-CompilerPass $Toolchain 'xelatex' 'XeLaTeX pass 2/3'
    Invoke-CompilerPass $Toolchain 'xelatex' 'XeLaTeX pass 3/3'
    if ($compilerRuns.Count -ne 4 -or @($compilerRuns | Where-Object {
            $_.exit_code -ne 0
        }).Count -ne 0) {
        throw 'Four successful compiler passes are required.'
    }
    $pdfPath = Get-PaperPath 'paper.pdf'
    if (-not (Test-Path -LiteralPath $pdfPath -PathType Leaf)) {
        throw "No PDF from this invocation: $pdfPath"
    }
    $pdf = Get-Item -LiteralPath $pdfPath
    if ($pdf.Length -eq 0 -or $pdf.LastWriteTimeUtc -lt $compileStarted) {
        throw "Empty or stale PDF: $pdfPath"
    }
    return Test-CompilerLogs $compileStarted
}

function Assert-PdfIdentity {
    param($Report)
    $actual = Get-FileRecord 'paper.pdf'
    if ($actual.sha256 -cne $Report.pdf.sha256 -or
            $actual.bytes -ne $Report.pdf.bytes) {
        throw 'PDF identity changed; all machine and visual checks are stale.'
    }
}

function Invoke-PdfTool {
    param($Toolchain, [string]$Name, [string[]]$Arguments)
    Assert-PdfIdentity $buildReport
    $result = Invoke-NativeCommand $Toolchain[$Name].path $Arguments
    Assert-CommandSucceeded $result
    $backend = $Name
    $popplerStatus = 'passed'
    $knownTags = @($buildReport.font_checks['adobe_gb1_tags'])
    if ($result.stderr -match '(?im)(Syntax (?:Error|Warning)|\bError:|' +
            'missing language pack|couldn.t (?:open|read)|failed to)') {
        if ($Name -eq 'pdfinfo' -or
                -not (Test-MissingLanguagePack $result.stderr $knownTags)) {
            $buildReport.pdf_tool_checks += [ordered]@{
                tool = $Name; status = 'failed'; stderr = $result.stderr.Trim()
                exit_code = $result.exit_code; arguments = @($Arguments)
            }
            Write-BuildReport $buildReport
            $count = @($result.stderr -split '\r?\n').Count
            throw (('PDF check failed in {0} ' +
                '({1} diagnostic lines; see report).') -f $Name, $count)
        }
        $popplerStatus = 'failed_superseded'
        $backend = 'pymupdf'
        $output = Invoke-PyMuPdfFallback $Toolchain $Name $Arguments
    }
    else { $output = $result.stdout }
    $buildReport.pdf_tool_checks += [ordered]@{
        tool = $Name; arguments = @($Arguments); status = $popplerStatus
        exit_code = $result.exit_code; stderr = $result.stderr.Trim()
        actual_backend = $backend; pdf_sha256 = $buildReport.pdf.sha256
    }
    Assert-PdfIdentity $buildReport
    return [ordered]@{ stdout = $output; backend = $backend }
}

function Test-MissingLanguagePack {
    param([string]$Diagnostic, [string[]]$KnownFontTags = @())
    $missing = "^Syntax Error: Missing language pack for 'Adobe-GB1' mapping$"
    $environment = '^(pdffonts|pdftotext|pdftoppm): ' +
        '(security risk: running with elevated privileges|' +
        'major issue: So far, you have not checked for MiKTeX updates\.)$'
    $lines = @($Diagnostic -split '\r?\n' | Where-Object { $_.Trim() })
    if (@($lines | Where-Object { $_ -cmatch $missing }).Count -eq 0) {
        return $false
    }
    $unknownCount = 0
    $showCount = 0
    foreach ($line in $lines) {
        if ($line -cmatch $missing -or $line -cmatch $environment) { continue }
        if ($line -cmatch "^Syntax Error: Unknown font tag '([^']+)'$") {
            if ($KnownFontTags -cnotcontains $matches[1]) { return $false }
            $unknownCount++
        }
        elseif ($line -cmatch '^Syntax Error \(\d+\): No font in show/space$') {
            $showCount++
        }
        else { return $false }
    }
    return $showCount -eq 0 -or $unknownCount -gt 0
}

function Get-PyMuPdfScript {
    return @'
import json, pathlib, re, sys
import pymupdf as p
mode, source, destination, number = sys.argv[1:]
document = p.open(source)
result = {"path": p.__file__, "version": p.VersionBind}
if mode == "pdffonts":
    fonts, resources, gb1_tags, other_tags = [], {}, set(), set()
    for page in document:
        for record in page.get_fonts(full=True):
            resources.setdefault(record[0], set()).add(record[4])
    for xref, tags in sorted(resources.items()):
        name, extension, kind, content = document.extract_font(xref)
        refs = re.findall(r"(\d+) 0 R",
                          document.xref_get_key(xref, "DescendantFonts")[1])
        gb1 = bool(refs) and all(
            document.xref_get_key(int(ref), "CIDSystemInfo/Registry")[1]
            == "Adobe" and
            document.xref_get_key(int(ref), "CIDSystemInfo/Ordering")[1]
            == "GB1" for ref in refs)
        if gb1:
            gb1_tags.update(tags)
        else:
            other_tags.update(tags)
        fonts.append({"xref": xref, "name": name, "type": kind,
                      "embedded": bool(content), "embedded_bytes": len(content),
                      "resource_tags": sorted(tags), "adobe_gb1": gb1})
    result["fonts"] = fonts
    result["adobe_gb1_tags"] = sorted(gb1_tags - other_tags)
elif mode == "pdftotext":
    text = "\n\f\n".join(page.get_text("text", sort=True)
                         for page in document)
    pathlib.Path(destination).write_text(text, encoding="utf-8", newline="\n")
elif mode == "pdftoppm":
    document[int(number)-1].get_pixmap(dpi=150, alpha=False).save(destination)
else:
    raise ValueError("Unsupported fallback operation: " + mode)
print(json.dumps(result, ensure_ascii=True))
'@
}

function Invoke-PyMuPdfFallback {
    param($Toolchain, [string]$Name, [string[]]$Arguments)
    # I01 permits this installed backend only for the exact Adobe-GB1 defect.
    $code = Get-PyMuPdfScript
    $destination = ''
    $page = '0'
    if ($Name -eq 'pdftotext') { $destination = $Arguments[-1] }
    if ($Name -eq 'pdftoppm') {
        $destination = $Arguments[-1] + '.png'
        $page = $Arguments[1]
    }
    $result = Invoke-NativeCommand $Toolchain.python.path @('-B', '-c', $code,
        $Name, (Get-PaperPath 'paper.pdf'), $destination, $page)
    Assert-CommandSucceeded $result
    if ($result.stderr.Trim()) {
        throw "PyMuPDF emitted a diagnostic: $($result.stderr.Trim())"
    }
    $summary = $result.stdout | ConvertFrom-Json
    $Toolchain['pymupdf'] = [ordered]@{
        path = $summary.path.Replace('\', '/')
        version = $summary.version; interpreter = $Toolchain.python.path
    }
    return $result.stdout
}

function Test-PdfMetadata {
    param($Toolchain)
    $pdfPath = Get-PaperPath 'paper.pdf'
    $probe = Invoke-PdfTool $Toolchain 'pdfinfo' `
        @('-rawdates', '-enc', 'UTF-8', $pdfPath)
    $metadata = [ordered]@{}
    foreach ($line in ($probe.stdout -split '\r?\n')) {
        if ($line -match '^([^:]+):\s*(.*)$') {
            $metadata[$matches[1]] = $matches[2]
        }
    }
    $pages = 0
    if (-not [int]::TryParse($metadata['Pages'], [ref]$pages) -or
            $pages -lt 1) {
        throw 'pdfinfo did not report a positive page count.'
    }
    $source = [IO.File]::ReadAllText((Get-PaperPath 'paper.tex'), $utf8)
    $title = [regex]::Match($source, '\\title\{([^{}]+)\}').Groups[1].Value
    $title = [regex]::Replace($title, '\s+', '')
    $actualTitle = [regex]::Replace($metadata['Title'], '\s+', '')
    if (-not $title -or $title -cne $actualTitle) {
        throw 'PDF metadata title does not match the current manuscript title.'
    }
    foreach ($field in @('Title', 'Author', 'Subject', 'Keywords')) {
        if (-not $metadata[$field] -or $metadata[$field] -match '\uFFFD') {
            throw "Missing or unreadable PDF metadata: $field"
        }
    }
    $sizes = Invoke-PdfTool $Toolchain 'pdfinfo' `
        @('-rawdates', '-enc', 'UTF-8', '-f', '1', '-l', "$pages", $pdfPath)
    $pageSizes = @([regex]::Matches($sizes.stdout,
        '(?m)^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts'))
    if ($pageSizes.Count -ne $pages) { throw 'Missing per-page paper sizes.' }
    foreach ($size in $pageSizes) {
        if ([double]$size.Groups[2].Value -ne 612 -or
                [double]$size.Groups[3].Value -ne 792) {
            throw "Page $($size.Groups[1].Value) is not Letter size."
        }
    }
    $buildReport.page_count = $pages
    return [ordered]@{
        status = 'passed'; backend = 'pdfinfo'; title_matches_source = $true
        all_pages_letter = $true; raw_pdf_dates = $true; values = $metadata
    }
}

function Test-PdfFonts {
    param($Toolchain)
    $probe = Invoke-PdfTool $Toolchain 'pdffonts' `
        @((Get-PaperPath 'paper.pdf'))
    $rows = @($probe.stdout -split '\r?\n' | Where-Object {
        $_ -match '\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$'
    })
    $fonts = @($rows | ForEach-Object {
        $tokens = $_.Trim() -split '\s+'
        [ordered]@{
            name = $tokens[0]; embedded = $tokens[-5] -eq 'yes'
            subset = $tokens[-4] -eq 'yes'; unicode = $tokens[-3] -eq 'yes'
            raw = $_.Trim()
        }
    })
    $tags = @()
    if ($probe.backend -eq 'pymupdf') {
        $summary = $probe.stdout | ConvertFrom-Json
        $fonts = @($summary.fonts)
        $tags = @($summary.adobe_gb1_tags)
    }
    if ($fonts.Count -eq 0) { throw 'PDF font check returned no font records.' }
    if (@($fonts | Where-Object { -not $_.embedded }).Count -gt 0) {
        throw 'The PDF contains unembedded fonts.'
    }
    return [ordered]@{
        status = 'passed'; backend = $probe.backend
        all_embedded = $true; fonts = $fonts; adobe_gb1_tags = $tags
        glyph_review_status = 'pending'
    }
}

function Test-PdfText {
    param($Toolchain)
    $relative = 'paper-output/revision-check/pdf-text.txt'
    Remove-PaperFile $relative
    $path = Get-PaperPath $relative
    $probe = Invoke-PdfTool $Toolchain 'pdftotext' `
        @('-enc', 'UTF-8', '-layout', (Get-PaperPath 'paper.pdf'), $path)
    $text = [IO.File]::ReadAllText($path, $utf8)
    $compact = [regex]::Replace($text, '\s+', '')
    $title = [regex]::Replace($buildReport.metadata_checks.values.Title,
        '\s+', '')
    $markers = [ordered]@{
        current_title = $compact.Contains($title)
        trial_count_120 = $compact -match '(?<!\d)120(?!\d)'
        minimum_keywords_054 = $compact.Contains('0.54')
        failed_gate = $compact -match
            '(\u9a8c\u6536|\u95e8\u69db).{0,30}\u5931\u8d25|' +
            '\u5931\u8d25.{0,30}(\u9a8c\u6536|\u95e8\u69db)|' +
            '(?i:acceptance.{0,30}failed|failed.{0,30}acceptance)'
        no_replacement_characters = -not $text.Contains([char]0xFFFD)
    }
    $failed = @($markers.GetEnumerator() | Where-Object { -not $_.Value })
    if ($failed.Count -gt 0) {
        throw "PDF text checks failed: $($failed.Name -join ', ')"
    }
    return [ordered]@{
        status = 'passed'; backend = $probe.backend
        artifact = (Get-FileRecord $relative)
        markers = $markers; contextual_claim_review_status = 'pending'
    }
}

function Invoke-PageRenders {
    param($Toolchain)
    for ($page = 1; $page -le $buildReport.page_count; $page++) {
        Write-Host "      Rendering page $page/$($buildReport.page_count)..."
        $prefix = 'paper-output/revision-check/page-{0:D3}' -f $page
        $relative = "$prefix.png"
        Remove-PaperFile $relative
        $probe = Invoke-PdfTool $Toolchain 'pdftoppm' @('-f', "$page",
            '-l', "$page", '-r', '150', '-singlefile', '-png',
            (Get-PaperPath 'paper.pdf'), (Get-PaperPath $prefix))
        $render = Get-FileRecord $relative
        if ($render.bytes -eq 0) { throw "Empty page render: $relative" }
        $buildReport.pages += [ordered]@{
            page_number = $page; render_path = $relative
            render_sha256 = $render.sha256; pdf_sha256 = $buildReport.pdf.sha256
            render_status = 'passed'; render_backend = $probe.backend
            visual_inspection_status = 'pending'
            inspected_at_utc = $null; inspection_notes = $null
        }
        Write-BuildReport $buildReport
    }
}

try {
    Write-Host '[1/5] Checking the complete toolchain...' -ForegroundColor Cyan
    $toolchain = Get-Toolchain
    Write-Host '[2/5] Checking generated evidence...' -ForegroundColor Cyan
    $evidenceCheck = Invoke-EvidenceCheck $toolchain
    $sourceRecords = @(Get-SourceRecords)
    Set-StaleReport 'A new build is starting; previous checks are invalid.'
    Write-Host '[3/5] Building a fresh PDF...' -ForegroundColor Cyan
    $logChecks = Invoke-Compilation $toolchain
    Assert-SourcesUnchanged $sourceRecords
    $buildReport = [ordered]@{
        schema_version = 'ara-paper-build-v1'
        status = 'pending'; stage = 'pending_machine_checks'
        build_started_at_utc = $buildStartedUtc.ToString('o')
        build_timestamp_utc = [DateTime]::UtcNow.ToString('o')
        tools = $toolchain; source_files = $sourceRecords
        evidence_check = $evidenceCheck
        compiler_runs = @($compilerRuns.ToArray())
        compiler_exit_codes = @($compilerRuns | ForEach-Object { $_.exit_code })
        compiler_checks = $logChecks; pdf = (Get-FileRecord 'paper.pdf')
        page_count = $null; font_checks = @{ status = 'pending' }
        text_checks = @{ status = 'pending' }
        metadata_checks = @{ status = 'pending' }; pages = @()
        pdf_tool_checks = @()
        source_reference_review_status = 'pending'
        visual_inspection_status = 'pending'; manual_review = $null
        tool_diagnostics = @($toolDiagnostics.ToArray())
    }
    Write-BuildReport $buildReport
    Write-Host '[4/5] Checking PDF metadata, fonts and text...' `
        -ForegroundColor Cyan
    $buildReport.metadata_checks = Test-PdfMetadata $toolchain
    Write-BuildReport $buildReport
    $buildReport.font_checks = Test-PdfFonts $toolchain
    Write-BuildReport $buildReport
    $buildReport.text_checks = Test-PdfText $toolchain
    Write-BuildReport $buildReport
    Write-Host '[5/5] Rendering every current PDF page...' -ForegroundColor Cyan
    Invoke-PageRenders $toolchain
    Assert-SourcesUnchanged $sourceRecords
    $buildReport.stage = 'pending_manual_review'
    $buildReport.tool_diagnostics = @($toolDiagnostics.ToArray())
    Write-BuildReport $buildReport
    Write-Host 'Machine checks completed; manual acceptance is pending.' `
        -ForegroundColor Green
    Write-Host "PDF:    $(Get-PaperPath 'paper.pdf')"
    Write-Host "Pages:  $($buildReport.page_count)"
    Write-Host "Bytes:  $($buildReport.pdf.bytes)"
    Write-Host "SHA256: $($buildReport.pdf.sha256)"
    Write-Host "Report: $(Get-PaperPath $reportRelativePath)"
    if ($Open) { Start-Process -FilePath (Get-PaperPath 'paper.pdf') }
}
catch {
    $failure = $_.Exception.Message
    try { Set-StaleReport $failure }
    catch { Write-Warning "Could not mark the old report stale: $_" }
    Write-Host "Build failed: $failure" -ForegroundColor Red
    exit 1
}
finally { $env:PATH = $originalPath }
