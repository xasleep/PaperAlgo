param(
    [switch]$SkipPlanning
)

$ErrorActionPreference = "Stop"

$GptVersion = if ($env:GPT_VERSION) { $env:GPT_VERSION } else { "deepseek-v4-pro" }

$PaperName = "Transformer"
$PdfLatexCleanedPath = "..\examples\Transformer_cleaned.tex"
$OutputDir = "..\outputs\Transformer"
$OutputRepoDir = "..\outputs\Transformer_repo"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputRepoDir | Out-Null

Write-Host $PaperName
Write-Host "------- PaperCoder -------"

if (-not $SkipPlanning) {
    python ..\codes\1_planning.py `
        --paper_name $PaperName `
        --gpt_version $GptVersion `
        --pdf_latex_path $PdfLatexCleanedPath `
        --paper_format LaTeX `
        --output_dir $OutputDir

    python ..\codes\1.1_extract_config.py `
        --paper_name $PaperName `
        --output_dir $OutputDir

    Copy-Item -Force -Path (Join-Path $OutputDir "planning_config.yaml") -Destination (Join-Path $OutputRepoDir "config.yaml")
} else {
    Write-Host "------- Skip Planning: reuse existing planning artifacts -------"
    if (-not (Test-Path (Join-Path $OutputDir "planning_config.yaml"))) {
        throw "planning_config.yaml not found. Run .\run_latex.ps1 without -SkipPlanning first."
    }
    Copy-Item -Force -Path (Join-Path $OutputDir "planning_config.yaml") -Destination (Join-Path $OutputRepoDir "config.yaml")
}

python ..\codes\2_analyzing.py `
    --paper_name $PaperName `
    --gpt_version $GptVersion `
    --pdf_latex_path $PdfLatexCleanedPath `
    --paper_format LaTeX `
    --output_dir $OutputDir

python ..\codes\3_coding.py `
    --paper_name $PaperName `
    --gpt_version $GptVersion `
    --pdf_latex_path $PdfLatexCleanedPath `
    --paper_format LaTeX `
    --output_dir $OutputDir `
    --output_repo_dir $OutputRepoDir
