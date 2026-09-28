# Project agent instructions

<!--
  Portable project instructions, installed by ai-stuff into a repository root.
  Replace the <placeholders> and delete sections that do not apply.
  Read by Codex, Antigravity, and opencode; CLAUDE.md imports it for Claude Code.
  Keep the "Subagent usage" section — it applies even when the
  dispatching-parallel-agents skill is not loaded.
-->

## Project

<What this project is, who it is for, and its current status (prototype / beta /
production). Note anything an agent must know before touching code.>

## Stack and layout

- <Languages, frameworks, package manager>
- <Where the important code lives; entry points>
- <Generated files and anything that must not be edited by hand>

## Commands

- Install: `<cmd>`
- Dev: `<cmd>`
- Test: `<cmd>`
- Lint / typecheck: `<cmd>`
- Build: `<cmd>`

Run the relevant checks before declaring work done.

## Coding style and rules

- Follow existing patterns and match the surrounding code.
- Do not over-rely on OOP when a simpler structure fits.
- Do not add helper functions when an equivalent already exists in a library
  that is already in use.
- Prefer the standard library and existing dependencies over new ones.
- Do not add comments unless asked.
- <Project-specific conventions>

## Testing and verification

- Write tests before implementation where practical.
- When a test fails, fix the code — not the test — unless the test asserts the
  wrong behavior. Never weaken a test just to make it pass.
- Static types, linters, and checkers are part of "done".

## Planning

- For large tasks, plan first with a strong model, then implement with cheaper
  models to save tokens.
- State the goal and the stopping condition before starting.

## Subagent usage

These rules are always in effect, even when the `dispatching-parallel-agents`
skill is not loaded. If you do dispatch subagents, follow them; the skill has
the full workflow.

- **Explicit opt-in only.** Never dispatch, spawn, or delegate to subagents
  unless the developer explicitly asks for it in the current task (e.g. "use
  subagents", "dispatch these in parallel", "delegate this"). A task looking
  parallelizable is not authorization, and neither is a past instruction. If
  parallel work would clearly help, ask once; absent a yes, do the work yourself,
  sequentially.
- **Cheap models for subagents.** The coordinating session runs the expensive
  model; helpers run a cheap/fast one: ChatGPT Luna, DeepSeek v4.1 Flash, Claude
  Sonnet, or Muse-Spark. Pick the cheapest that can do the job, never silently a
  premium model, and state the chosen model in the dispatch and the summary.
- **Poll at the harness cadence.** Polling too often destroys prompt-cache
  retention. Claude Code: check every 4 minutes. Codex and opencode running
  OpenAI models: check every 25 minutes, otherwise wait for completion, and do
  not poll faster. Manual polling only — no tight auto-poll loops. If a result
  arrives earlier, use it immediately.
