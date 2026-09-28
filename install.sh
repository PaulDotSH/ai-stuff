#!/usr/bin/env bash
#
# Install skills from this repo into AI harness skill directories.
#
# Skills live in ./skills/<name>/SKILL.md. This script copies (or symlinks) each
# skill folder into the directory a given harness scans. .agents/skills is the
# cross-harness standard (Codex, Antigravity, opencode); Claude Code uses
# .claude/skills.
#
# Usage:
#   ./install.sh [--harness H]... [--scope project|global] [--project-dir DIR]
#                [--skill NAME]... [--agents] [--link] [--force] [--dry-run]
#
#   H in: agents codex claude opencode antigravity all   (default: agents)
#   --agents installs the agents/ project template into --project-dir.
#
# Examples:
#   ./install.sh --scope global
#   ./install.sh --harness all --scope project --project-dir .
#   ./install.sh --harness claude --skill brainstorming --link --force
#   ./install.sh --agents --project-dir /path/to/new-repo
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="$REPO_ROOT/skills"

HARNESSES=()
SCOPE="global"
PROJECT_DIR="$(pwd)"
SELECTED_SKILLS=()
LINK=0
FORCE=0
DRY_RUN=0
AGENTS=0

usage() {
  awk 'NR>2 && /^set /{exit} NR>2{sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness|-H)
      IFS=',' read -r -a _h <<< "$2"
      for _x in "${_h[@]}"; do HARNESSES+=("$_x"); done
      shift 2 ;;
    --scope)       SCOPE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --skill|-s)
      IFS=',' read -r -a _s <<< "$2"
      for _x in "${_s[@]}"; do SELECTED_SKILLS+=("$_x"); done
      shift 2 ;;
    --link|-l)     LINK=1; shift ;;
    --force|-f)    FORCE=1; shift ;;
    --agents)      AGENTS=1; shift ;;
    --dry-run|-n)  DRY_RUN=1; shift ;;
    --help|-h)     usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

# Default harness when none was requested.
if [[ ${#HARNESSES[@]} -eq 0 ]]; then
  HARNESSES=(agents)
fi

# Expand 'all'.
_expanded=()
for h in "${HARNESSES[@]}"; do
  if [[ "$h" == "all" ]]; then
    _expanded+=(agents codex claude opencode antigravity)
  else
    _expanded+=("$h")
  fi
done
HARNESSES=("${_expanded[@]}")

[[ -d "$SKILLS_ROOT" ]] || { echo "No skills directory at $SKILLS_ROOT" >&2; exit 1; }

case "$SCOPE" in
  global)  BASE_DIR="$HOME" ;;
  project) BASE_DIR="$(cd "$PROJECT_DIR" && pwd)" ;;
  *) echo "Invalid scope: $SCOPE (use project|global)" >&2; exit 1 ;;
esac

target_dir() {
  case "$1" in
    agents|codex)  echo "$BASE_DIR/.agents/skills" ;;
    claude)        echo "$BASE_DIR/.claude/skills" ;;
    opencode)
      if [[ "$SCOPE" == "global" ]]; then echo "$BASE_DIR/.config/opencode/skills"
      else echo "$BASE_DIR/.opencode/skills"; fi ;;
    antigravity)
      if [[ "$SCOPE" == "global" ]]; then echo "$BASE_DIR/.gemini/config/skills"
      else echo "$BASE_DIR/.agents/skills"; fi ;;
    *) echo "Unknown harness: $1" >&2; return 1 ;;
  esac
}

# Resolve requested skills.
if [[ ${#SELECTED_SKILLS[@]} -gt 0 ]]; then
  SKILLS=("${SELECTED_SKILLS[@]}")
else
  SKILLS=()
  for d in "$SKILLS_ROOT"/*/; do
    if [[ -f "${d}SKILL.md" ]]; then
      SKILLS+=("$(basename "$d")")
    fi
  done
fi
[[ ${#SKILLS[@]} -gt 0 ]] || { echo "No skills found in $SKILLS_ROOT" >&2; exit 1; }

for name in "${SKILLS[@]}"; do
  [[ -f "$SKILLS_ROOT/$name/SKILL.md" ]] || { echo "Skill not found: $name" >&2; exit 1; }
done

echo "Repo:    $REPO_ROOT"
echo "Scope:   $SCOPE ($BASE_DIR)"
echo "Skills:  ${SKILLS[*]}"
echo ""

# Unique target dirs, preserving first-seen order (no assoc arrays: bash 3.2 safe).
TARGETS=()
for h in "${HARNESSES[@]}"; do
  d="$(target_dir "$h")"
  found=0
  for t in "${TARGETS[@]:-}"; do
    if [[ "$t" == "$d" ]]; then found=1; break; fi
  done
  if [[ "$found" -eq 0 ]]; then TARGETS+=("$d"); fi
done

for target in "${TARGETS[@]}"; do
  echo "Target: $target"
  for name in "${SKILLS[@]}"; do
    src="$SKILLS_ROOT/$name"
    dest="$target/$name"

    if [[ -e "$dest" && "$FORCE" -ne 1 ]]; then
      echo "  skip  $name (exists; use --force)"
      continue
    fi
if [[ "$AGENTS" -eq 1 ]]; then
  TEMPLATE_DIR="$REPO_ROOT/agents"
  TEMPLATE_DEST="$(cd "$PROJECT_DIR" && pwd)"
  echo "Template: $TEMPLATE_DEST (from agents/)"

  for file in AGENTS.md CLAUDE.md; do
    src="$TEMPLATE_DIR/$file"
    dest="$TEMPLATE_DEST/$file"

    if [[ ! -f "$src" ]]; then
      echo "  warn  missing template file: $src" >&2
      continue
    fi
    if [[ -e "$dest" && "$FORCE" -ne 1 ]]; then
      echo "  skip  $file (exists; use --force)"
      continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  plan  $file -> $dest"
      continue
    fi
    cp "$src" "$dest"
    echo "  copy  $file"
  done
  echo ""
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  plan  $name -> $dest"
      continue
    fi

    mkdir -p "$target"
    if [[ -e "$dest" ]]; then rm -rf "$dest"; fi

    if [[ "$LINK" -eq 1 ]] && ln -sfn "$src" "$dest" 2>/dev/null; then
      echo "  link  $name"
    else
      cp -R "$src" "$dest"
      echo "  copy  $name"
    fi
  done
  echo ""
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete. No files were changed."
else
  echo "Done."
fi
