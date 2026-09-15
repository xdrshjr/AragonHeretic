[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Open
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$mainFileName = 'paper.tex'
$pdfFileName = 'paper.pdf'
$logFileName = 'paper.log'

function Invoke-TeXCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        $commandName = Split-Path -Leaf $Executable
        throw "$commandName failed with exit code $LASTEXITCODE."
    }
}

try {
    Write-Host '[1/3] Checking the TeX toolchain...' -ForegroundColor Cyan

    # The MiKTeX installer may not update the current PowerShell session's PATH.
    $candidateBinDirectories = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\MiKTeX\miktex\bin\x64'),
        (Join-Path $env:ProgramFiles 'MiKTeX\miktex\bin\x64')
    )
    if (Test-Path -LiteralPath 'C:\texlive' -PathType Container) {
        $candidateBinDirectories += Get-ChildItem -LiteralPath 'C:\texlive' -Directory |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'bin\windows' }
    }
    foreach ($directory in ($candidateBinDirectories | Select-Object -Unique)) {
        if ((Test-Path -LiteralPath $directory -PathType Container) -and
            (($env:PATH -split ';') -notcontains $directory)) {
            $env:PATH = "$directory;$env:PATH"
        }
    }

    $missingTools = @()
    $toolPaths = @{}
    foreach ($tool in @('xelatex', 'bibtex')) {
        $command = Get-Command -Name $tool -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1

        if ($null -eq $command) {
            $missingTools += $tool
        }
        else {
            $toolPaths[$tool] = $command.Path
        }
    }

    if ($missingTools.Count -gt 0) {
        throw ('Missing TeX tools: {0}. Install TeX Live (recommended) or MiKTeX.' -f ($missingTools -join ', '))
    }

    $mainFilePath = Join-Path $projectRoot $mainFileName
    if (-not (Test-Path -LiteralPath $mainFilePath -PathType Leaf)) {
        throw "Main document not found: $mainFilePath"
    }

    Push-Location -LiteralPath $projectRoot
    try {
        if ($Clean) {
            Write-Host '      Cleaning previous build outputs...' -ForegroundColor Cyan
            $generatedExtensions = @(
                'aux', 'bbl', 'blg', 'dvi', 'fdb_latexmk', 'fls', 'log',
                'out', 'pdf', 'run.xml', 'synctex.gz', 'xdv'
            )
            foreach ($extension in $generatedExtensions) {
                $generatedFile = Join-Path $projectRoot "paper.$extension"
                if (Test-Path -LiteralPath $generatedFile -PathType Leaf) {
                    Remove-Item -LiteralPath $generatedFile -Force
                }
            }
        }

        Write-Host '[2/3] Compiling PDF (XeLaTeX pass 1/3)...' -ForegroundColor Cyan
        Invoke-TeXCommand -Executable $toolPaths['xelatex'] -Arguments @(
            '-interaction=nonstopmode',
            '-halt-on-error',
            '-file-line-error',
            $mainFileName
        )

        Write-Host '      Building the bibliography...' -ForegroundColor Cyan
        Invoke-TeXCommand -Executable $toolPaths['bibtex'] -Arguments @('paper')

        foreach ($pass in 2..3) {
            Write-Host "      Compiling PDF (XeLaTeX pass $pass/3)..." -ForegroundColor Cyan
            Invoke-TeXCommand -Executable $toolPaths['xelatex'] -Arguments @(
                '-interaction=nonstopmode',
                '-halt-on-error',
                '-file-line-error',
                $mainFileName
            )
        }

        $pdfPath = Join-Path $projectRoot $pdfFileName
        if (-not (Test-Path -LiteralPath $pdfPath -PathType Leaf)) {
            throw "latexmk completed but did not create $pdfPath"
        }

        Write-Host '[3/3] Checking the build log...' -ForegroundColor Cyan
        $logPath = Join-Path $projectRoot $logFileName
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            $warningPatterns = @(
                'undefined',
                'Overfull \\[hv]box',
                'Missing character',
                'font.*substitut'
            )
            $logWarnings = Select-String -LiteralPath $logPath -Pattern $warningPatterns |
                ForEach-Object { '{0}:{1}' -f $_.LineNumber, $_.Line.Trim() } |
                Select-Object -Unique

            if ($logWarnings) {
                Write-Warning "The PDF was created, but the log contains warnings:"
                $logWarnings | ForEach-Object { Write-Warning $_ }
            }
        }

        $pdf = Get-Item -LiteralPath $pdfPath
        $hash = (Get-FileHash -LiteralPath $pdfPath -Algorithm SHA256).Hash
        Write-Host ''
        Write-Host 'Build succeeded.' -ForegroundColor Green
        Write-Host "PDF:    $($pdf.FullName)"
        Write-Host ('Size:   {0:N0} bytes' -f $pdf.Length)
        Write-Host "SHA256: $hash"

        if ($Open) {
            Start-Process -FilePath $pdfPath
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Host ''
    Write-Host "Build failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
