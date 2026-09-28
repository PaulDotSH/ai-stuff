<#
.SYNOPSIS
  Install skills from this repo into AI harness skill directories.

.DESCRIPTION
  Skills live in ./skills/<name>/SKILL.md. This script copies (or symlinks) each
  skill folder into the directory a given harness scans. .agents/skills is the
  cross-harness standard (Codex, Antigravity, opencode); Claude Code uses
  .claude/skills.

.EXAMPLE
  pwsh -File install.ps1 -Scope global

.EXAMPLE
  pwsh -File install.ps1 -Harness all -Scope project -ProjectDir .

.EXAMPLE
  pwsh -File install.ps1 -Harness claude -Skill brainstorming -Link -Force

.EXAMPLE
  # New repo: project skills + project-instruction template
  pwsh -File install.ps1 -Harness all -Scope project -Agents -ProjectDir C:\path\to\new-repo
#>
[CmdletBinding()]
param(
  [ValidateSet('agents', 'codex', 'claude', 'opencode', 'antigravity', 'all')]
  [string[]]$Harness = @('agents'),

  [ValidateSet('project', 'global')]
  [string]$Scope = 'global',

  [string]$ProjectDir = (Get-Location).Path,

  # Skill folder names to install. Default: every skill in ./skills.
  [string[]]$Skill,

  # Create symlinks instead of copying (needs Developer Mode / admin on Windows).
  [switch]$Link,

  # Overwrite existing skill folders.
  [switch]$Force,

  # Also install the agents/ project-instruction template into -ProjectDir.
  [switch]$Agents,

  # Print planned actions without touching the filesystem.
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepoRoot   = $PSScriptRoot
$SkillsRoot = Join-Path $RepoRoot 'skills'
$HomeDir    = if ($HOME) { $HOME } else { [Environment]::GetFolderPath('UserProfile') }
$BaseDir    = if ($Scope -eq 'global') { $HomeDir } else { (Resolve-Path -LiteralPath $ProjectDir).Path }

if (-not (Test-Path -LiteralPath $SkillsRoot)) {
  throw "No skills directory found at $SkillsRoot"
}

if ($Harness -contains 'all') {
  $Harness = @('agents', 'codex', 'claude', 'opencode', 'antigravity')
}

function Get-TargetDir {
  param([string]$Name)

  switch ($Name) {
    'agents'      { Join-Path $BaseDir '.agents/skills' }
    'codex'       { Join-Path $BaseDir '.agents/skills' }
    'claude'      { Join-Path $BaseDir '.claude/skills' }
    'opencode'    { if ($Scope -eq 'global') { Join-Path $BaseDir '.config/opencode/skills' } else { Join-Path $BaseDir '.opencode/skills' } }
    'antigravity' { if ($Scope -eq 'global') { Join-Path $BaseDir '.gemini/config/skills' } else { Join-Path $BaseDir '.agents/skills' } }
    default       { throw "Unknown harness: $Name" }
  }
}

# Resolve requested skills.
$AllSkills = Get-ChildItem -LiteralPath $SkillsRoot -Directory |
  Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') }

if ($Skill) {
  $Selected = foreach ($s in $Skill) {
    $match = $AllSkills | Where-Object { $_.Name -eq $s }
    if (-not $match) { throw "Skill not found: $s" }
    $match
  }
} else {
  $Selected = $AllSkills
}

if (-not $Selected) { throw "No skills found in $SkillsRoot" }

# Resolve unique target directories across the requested harnesses.
$Targets = [ordered]@{}
foreach ($h in $Harness) {
  $dir = [System.IO.Path]::GetFullPath((Get-TargetDir -Name $h))
  if (-not $Targets.Contains($dir)) { $Targets[$dir] = @() }
  $Targets[$dir] += $h
}

Write-Host "Repo:    $RepoRoot"
Write-Host "Scope:   $Scope ($BaseDir)"
Write-Host "Skills:  $($Selected.Name -join ', ')"
Write-Host ""

foreach ($target in $Targets.Keys) {
  $via = ($Targets[$target] | Sort-Object -Unique) -join ', '
  Write-Host "Target [$via]: $target"

  foreach ($skillDir in $Selected) {
    $dest = Join-Path $target $skillDir.Name
    $exists = Test-Path -LiteralPath $dest

    if ($exists -and -not $Force) {
      Write-Host "  skip  $($skillDir.Name) (exists; use -Force)"
      continue
    }
    if ($DryRun) {
      Write-Host "  plan  $($skillDir.Name) -> $dest"
      continue
    }

    if (-not (Test-Path -LiteralPath $target)) {
      New-Item -ItemType Directory -Force -Path $target | Out-Null
    }
    if ($exists) { Remove-Item -LiteralPath $dest -Recurse -Force }

    if ($Link) {
      try {
        New-Item -ItemType SymbolicLink -Path $dest -Target $skillDir.FullName | Out-Null
        Write-Host "  link  $($skillDir.Name)"
      } catch {
        Write-Warning "  symlink failed for $($skillDir.Name) ($($_.Exception.Message)); copying instead"
        Copy-Item -LiteralPath $skillDir.FullName -Destination $dest -Recurse -Force
        Write-Host "  copy  $($skillDir.Name)"
      }
    } else {
      Copy-Item -LiteralPath $skillDir.FullName -Destination $dest -Recurse -Force
      Write-Host "  copy  $($skillDir.Name)"
    }
  }
  Write-Host ""
}

if ($Agents) {
  $TemplateDir  = Join-Path $RepoRoot 'agents'
  $TemplateDest = (Resolve-Path -LiteralPath $ProjectDir).Path
  Write-Host "Template: $TemplateDest (from agents/)"

  foreach ($file in @('AGENTS.md', 'CLAUDE.md')) {
    $src  = Join-Path $TemplateDir $file
    $dest = Join-Path $TemplateDest $file

    if (-not (Test-Path -LiteralPath $src)) {
      Write-Warning "  missing template file: $src"
      continue
    }
    if ((Test-Path -LiteralPath $dest) -and -not $Force) {
      Write-Host "  skip  $file (exists; use -Force)"
      continue
    }
    if ($DryRun) {
      Write-Host "  plan  $file -> $dest"
      continue
    }
    Copy-Item -LiteralPath $src -Destination $dest -Force
    Write-Host "  copy  $file"
  }
  Write-Host ""
}

if ($DryRun) { Write-Host "Dry run complete. No files were changed." }
else { Write-Host "Done." }
