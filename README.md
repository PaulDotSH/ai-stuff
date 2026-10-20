# ai-stuff

Personal, portable AI agent configuration: canonical instructions plus
installable skills that work across opencode, Claude Code, Codex, and
Antigravity.

## What's here

```
ai-stuff/
  AGENTS.md      # this repo's instructions (single source of truth)
  CLAUDE.md      # @AGENTS.md import for Claude Code
  README.md      # this file
  agents/
    AGENTS.md    # portable project-instruction template for new repos
    CLAUDE.md    # @AGENTS.md import for Claude Code
  notes/
    *.md         # reference notes, e.g. postgres-performance-design.md
  skills/
    <name>/
      SKILL.md   # required — name + description frontmatter
      ...        # optional scripts/ and references, self-contained
  install.ps1    # Windows installer
  install.sh     # macOS / Linux installer
```

`SKILL.md` is an open, agent-neutral format. The only harness-specific detail is
the directory the skill is installed into.

## Where skills are read from

| Harness         | Project                 | Global                            |
| --------------- | ----------------------- | --------------------------------- |
| Codex           | `<repo>/.agents/skills` | `~/.agents/skills`                |
| Antigravity 2.0 | `<repo>/.agents/skills` | `~/.gemini/config/skills`         |
| opencode        | `.agents/skills` or `.opencode/skills` | `~/.agents/skills` or `~/.config/opencode/skills` |
| Claude Code     | `<repo>/.claude/skills` | `~/.claude/skills`                |

`.agents/skills/` is the cross-harness standard and the default install target —
it covers Codex, Antigravity (workspace), and opencode in one shot. Claude Code
needs `.claude/skills`.

## Install

```powershell
# Windows — global, universal .agents location
pwsh -File install.ps1 -Scope global

# Windows — every harness, project scope
pwsh -File install.ps1 -Harness all -Scope project -ProjectDir .

# Windows — one skill, symlink instead of copy
pwsh -File install.ps1 -Harness claude -Skill brainstorming -Link

# Windows — set up a new repo: skills + AGENTS.md/CLAUDE.md template
pwsh -File install.ps1 -Harness all -Scope project -Agents -ProjectDir C:\path\to\new-repo
```

```bash
# macOS / Linux
./install.sh --scope global
./install.sh --harness all --scope project --project-dir .
./install.sh --harness claude --skill brainstorming --link
./install.sh --agents --project-dir /path/to/new-repo
```

Preview with `-DryRun` / `--dry-run`; overwrite with `-Force` / `--force`.

On Windows, a global install (`-Scope global`) also sets the user environment
variable `PYTHONUTF8=1` if it isn't set. Without it Python writes stdout as cp1252,
and agents crash with `UnicodeEncodeError` when printing non-ASCII text. Isolated
runs (`python -I`) ignore the variable, so pass `python -X utf8` there.

## Project template

`agents/AGENTS.md` is the template installed into a new repository root. It
carries the project-description/style/testing boilerplate plus the always-on
**subagent usage** policy (opt-in only, cheap models, per-harness polling
cadence) — so those rules still apply when the skill is not loaded. `agents/CLAUDE.md`
is a one-line `@AGENTS.md` import for Claude Code.

Install it alongside skills with `-Agents` / `--agents`. It writes `AGENTS.md`
and `CLAUDE.md` into `-ProjectDir` and skips files that already exist unless
`-Force` / `--force` is given.

## RTK

This repo is set up for [RTK](https://github.com/rtk-ai/rtk), the CLI proxy that
condenses shell output before the agent reads it. Global hooks are installed for
Claude Code, Codex, and opencode; because Antigravity is project-scoped, this
repo also carries a project rule at `.agents/rules/antigravity-rtk-rules.md`,
plus a Codex project hook (`.codex/hooks.json`) and `RTK.md` (imported from
`AGENTS.md`).

Re-run or remove with `rtk init --agent antigravity` / `rtk init --codex --uninstall`,
or `rtk init -g --uninstall` for the global hooks. Requires `rtk` on PATH.

## Skills

| Skill                       | Description                                                                 | Source |
| --------------------------- | --------------------------------------------------------------------------- | ------ |
| brainstorming               | Turns ideas into designs/specs through collaborative dialogue; gates implementation behind design approval. | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/brainstorming) |
| dispatching-parallel-agents | Dispatch subagents for independent tasks — only on explicit developer request, on cheap models, polled at a cache-safe cadence per harness. Locally modified. | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/dispatching-parallel-agents) |
| requesting-code-review      | Request an independent subagent review of changes before merge. Dispatches a reviewer, so the opt-in and cheap-model rules apply. | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/requesting-code-review) |
| receiving-code-review       | Evaluate incoming review feedback with technical rigor — verify before implementing, push back with reasoning, skip performative agreement. | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/receiving-code-review) |
| codebase-design             | Shared vocabulary (module / interface / seam / adapter / depth) for designing deep modules; used inside other design work. Locally modified. | [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/engineering/codebase-design) |
| grilling                    | Relentlessly interview the user, frontier-by-frontier, to stress-test a plan or decision they already have. Locally modified. | [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling) |
| handoff                     | Compact the current conversation into a handoff document a fresh agent can pick up. Manual invocation. Locally modified. | [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/productivity/handoff) |
| to-questionnaire            | Turn a decision the user can't answer into a questionnaire for someone who holds the knowledge. Locally modified. | [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/productivity/to-questionnaire) |
| improve-codebase-architecture | Scan a codebase for deepening opportunities, report them as HTML, then grill through the chosen one. Locally modified. | [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture) |
| frontend-design             | Distinctive, intentional visual design for new or reshaped UI — avoids templated AI defaults. | [anthropics/skills](https://github.com/anthropics/skills/tree/main/skills/frontend-design) |
| ponytail                    | Forces the laziest working solution (YAGNI → stdlib → native → existing dep → one line). Opt-in mode; locally modified. | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) |
| remotion-motion-graphics    | Create/edit motion-graphics videos with Remotion (springs, stagger, grade, grain, captions) and a mandatory render→inspect→fix loop. Locally modified. | [haidrrrry/claude-remotion-skill](https://github.com/haidrrrry/claude-remotion-skill) |
| unity-mcp-skill             | Orchestrate the Unity Editor through MCP for Unity tools/resources — scenes, GameObjects, scripts, tests. | [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp) |

`brainstorming` references a `writing-plans` skill for its architectural path
and a `visual-companion` browser server (Node.js) for visual questions. Those
support files ship inside the skill folder; the harness just needs to reach them.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with `name` + `description` frontmatter
   (`name` must match the folder name).
2. Put every file the skill needs inside its own folder so relative paths keep
   working after install.
3. If it's imported, keep upstream content intact and add a row to the Skills
   table with its source.
4. Verify: `pwsh -File install.ps1 -DryRun -Harness all` (or `./install.sh --dry-run`).

## Notes

### General knowledge
When starting to use an agent in a repository that wasn't setup /init is helpful because it scans the codebase and generates a markdown pointing to useful parts of the codebase, so a complete scan isn't always required, claude code uses claude.md, other harnesses use agents.md, an easy fix is using agents.md and importing it from claude.md

### Agents.md / Claude.md

Agents.md should contain a project description, current status, coding style and rules, such as not over-relying on OOP or not implementing helper functions if there are existing variants in used libraries.

### Skills
Skills are similar to agents.md but for repeatable tasks, they are dynamically or manually loaded, do not use a ton of skills if possible

### Planning

Planning is extremely useful on big tasks, using a smart model for planning and smaller models for implementation is a good idea to save on tokens

### Testing / Verification
In prompts it's useful to tell claude a proper goal, when to stop, writing tests before implementation is also great, since models tend to write tests to already pass the code, and they will modify the test instead of the code when one is failing, static typed languages also help, same with linters and other checkers

### MCPs

MCPs fill the context window extremely fast, have them enabled only when you need them in the current task, some useful ones are ghidra_mcp, jadx_mcp, dnspy for reverse engineering, some are useful based on the task such as MCPs for photoshop and unity.

Filesystem mcp is good in general depending on the harness and model, and same for webclaw and playwright if the model doesn't support something similar native

### General commands

/compact is useful when the token count is high, since models degrade after a context size

### Claude commands

/btw is useful for asking question based on the current chat without interrupting the model, most models tend to lose focus on the task when interrupted

### Context window

After ~200k tokens models get "dumber", start new chats or use /compact if possible
