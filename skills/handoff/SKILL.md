---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work. Save to the temporary directory of the user's OS - not the current workspace.

## Scope and boundaries

- This is a manual, user-invoked action for carrying context between sessions. It designs nothing, decides nothing, and implements nothing.
- Reference existing artifacts (specs, plans, ADRs, issues, commits, diffs) by path or URL; do not restate them. Those design artifacts are produced elsewhere, e.g. by `brainstorming`.
- In the "suggested skills" section, name skills by their canonical installed name (e.g. `brainstorming`, `codebase-design`, `requesting-code-review`, `grilling`).
- Manual invocation only: Claude Code honors `disable-model-invocation`; in other harnesses, invoke this skill deliberately.

Include a "suggested skills" section in the document, naming which skills the next agent should call the Skill tool for.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.

## Local modifications

Imported from [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/productivity/handoff). Changed here: added the **Scope and boundaries** section (reference-don't-duplicate, canonical skill names, manual invocation).
