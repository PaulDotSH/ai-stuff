# AI Stuff — agent instructions

This repo is the single source of truth for portable AI agent configuration:
canonical instructions plus installable skills. Everything here is designed to
be copied or linked into the skill directory of any harness.

## What this repo is

- `skills/` — portable `SKILL.md` packages (the actual content).
- `agents/` — portable project-instruction template installed into new repos.
- `AGENTS.md` — this file, the canonical project/agent instructions.
- `CLAUDE.md` — one-line import of `AGENTS.md` for Claude Code.
- `install.ps1` / `install.sh` — install skills and the project template.

## Layout

```
ai-stuff/
  AGENTS.md
  CLAUDE.md
  README.md
  agents/
    AGENTS.md             # portable template installed into new repos
    CLAUDE.md             # @AGENTS.md import for Claude Code
  skills/
    <skill-name>/
      SKILL.md              # required
      scripts/              # optional helpers
      *.md                  # optional references
  tools/
    DEPLOY.md             # manifest of machine-local tools; rest is gitignored
  install.ps1               # Windows installer
  install.sh                # macOS/Linux installer
```

## Where skills live per harness

`SKILL.md` is an open, agent-neutral format. Only the directory changes.

| Harness             | Project                    | Global                          |
| ------------------- | -------------------------- | ------------------------------- |
| Codex               | `<repo>/.agents/skills`    | `~/.agents/skills`              |
| Antigravity 2.0     | `<repo>/.agents/skills`    | `~/.gemini/config/skills`       |
| opencode            | `.agents/skills` or `.opencode/skills` | `~/.agents/skills` or `~/.config/opencode/skills` |
| Claude Code         | `<repo>/.claude/skills`    | `~/.claude/skills`              |

`.agents/skills/` is the cross-harness standard and is the default target — it
is read by Codex, Antigravity, and opencode. Claude Code needs `.claude/skills`.

## Install

```powershell
# Windows — global, universal .agents location
pwsh -File install.ps1 -Scope global

# Windows — all harnesses, project scope
pwsh -File install.ps1 -Harness all -Scope project -ProjectDir .

# Windows — specific skill, symlink instead of copy
pwsh -File install.ps1 -Harness claude -Skill brainstorming -Link
```

```bash
# macOS / Linux
./install.sh --scope global
./install.sh --harness all --scope project --project-dir .
./install.sh --harness claude --skill brainstorming --link
```

Use `-DryRun` / `--dry-run` to preview. `-Force` / `--force` overwrites.

## Skill authoring rules

- One directory per skill: `skills/<name>/SKILL.md`.
- Frontmatter must include `name` and `description`. opencode recognizes only
  those plus optional `license`, `compatibility`, `metadata`; harness-specific
  fields from upstream (e.g. Claude Code's `disable-model-invocation` and
  `argument-hint`) are preserved verbatim and ignored by harnesses that don't
  know them.
- `name` must be lowercase, hyphen-separated, and **match the directory name**
  (`^[a-z0-9]+(-[a-z0-9]+)*$`).
- `description` must be 1–1024 chars and say both what the skill does and when
  to use it — that is all the agent sees when choosing.
- Keep relative references (`scripts/...`, `*.md`) working after install. Every
  file a skill needs must live inside its own directory.
- Imported skills keep their upstream content and add a source link in this
  file's "Skills" table. Do not silently rewrite upstream instructions; if you
  adapt one, mark the change in the file and note it in the Skills table.

## Rules

- Do not commit secrets, tokens, or absolute personal paths to this repo.
- Do not install skills into a harness from inside a skill; use the installer.
- Prefer adding one well-scoped skill over many overlapping ones.
- Test an install with `-DryRun` before changing a live harness directory.

## Subagent usage

The canonical, installable statement of the subagent policy lives in
`agents/AGENTS.md` — the template dropped into new repositories, so the rules
survive even when the `dispatching-parallel-agents` skill is not loaded. The
same three rules apply when working in this repo: explicit developer opt-in
only, cheap models for subagents, and per-harness polling cadence.

## Skills

| Skill                       | Source                                                   | Notes |
| --------------------------- | -------------------------------------------------------- | ----- |
| brainstorming               | https://github.com/obra/superpowers (skills/brainstorming) | Upstream, unchanged. |
| dispatching-parallel-agents | https://github.com/obra/superpowers (skills/dispatching-parallel-agents) | Locally modified: explicit developer opt-in gate, cheap-model mandate for subagents, per-harness polling cadence. Changes are marked in the file. |
| requesting-code-review      | https://github.com/obra/superpowers (skills/requesting-code-review) | Upstream, unchanged. Dispatches a reviewer, so the subagent opt-in and cheap-model rules in `agents/AGENTS.md` apply. |
| receiving-code-review       | https://github.com/obra/superpowers (skills/receiving-code-review) | Upstream, unchanged. |
| codebase-design             | https://github.com/mattpocock/skills (skills/engineering/codebase-design) | Locally modified: scope boundary vs `brainstorming`; DESIGN-IT-TWICE obeys the subagent policy. |
| grilling                    | https://github.com/mattpocock/skills (skills/productivity/grilling) | Locally modified: scope boundary vs `brainstorming`/`to-questionnaire`; fact-finding obeys the subagent policy. |
| handoff                     | https://github.com/mattpocock/skills (skills/productivity/handoff) | Locally modified: scope boundary added; manual invocation. |
| to-questionnaire            | https://github.com/mattpocock/skills (skills/productivity/to-questionnaire) | Locally modified: scope boundary vs `grilling`. |
| improve-codebase-architecture | https://github.com/mattpocock/skills (skills/engineering/improve-codebase-architecture) | Locally modified: scope boundary vs `codebase-design`/`brainstorming`; codebase walk obeys the subagent policy; manual invocation. |
| frontend-design             | https://github.com/anthropics/skills (skills/frontend-design) | Upstream, unchanged. |
| ponytail                    | https://github.com/DietrichGebert/ponytail (skills/ponytail) | Locally modified: scoped as an opt-in mode against the AGENTS.md baseline; harness-command assumptions removed. |
| remotion-motion-graphics    | https://github.com/haidrrrry/claude-remotion-skill (remotion-motion-graphics) | Locally modified: render/still from a single `remotion bundle --out-dir build` instead of the entry file, so each run stops leaving a multi-GB `%TEMP%emotion-webpack-bundle-*` copy of `public/`. Ships a `.skill` bundle for Claude Desktop; render-inspect loop depends on ffmpeg/Remotion being installed. |
| unity-mcp-skill             | https://github.com/CoplayDev/unity-mcp | Upstream, unchanged. Installed and kept in sync by the MCP for Unity package (`.unity-mcp-skill-sync` marker); frontmatter name is `unity-mcp-orchestrator`. |

## Command output

@RTK.md

### Windows

RTK rewrites Linux-style commands (`grep`, `ls`, `cat`, `head`, `tail`, `wc`,
`rg`, `find`) to its own `rtk` subcommands. Some are native; others proxy the
Unix tool, so on Windows those tools must be on PATH:

- GNU `ls`, `grep`, `wc`, `head`, `tail`, `sed`, `awk` come from Git for Windows
  (`C:\Program Files\Git\usr\bin`, added to PATH).
- `rg` comes from ripgrep (`winget install BurntSushi.ripgrep.MSVC`).

Native and always safe on Windows: `rtk read`, `rtk find`, `rtk diff`, `rtk json`,
`rtk log`, `rtk env`. Prefer those (or `rtk rg`) over `rtk grep` / `rtk ls` when
in doubt. `sed` / `awk` are not rewritten and still need the Git tools on PATH.
Restart the shell after PATH changes.
