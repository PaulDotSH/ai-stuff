---
name: grilling
description: Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases.
---

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

## Scope and boundaries

This skill stress-tests a plan, decision, or idea the user **already has**. Its output is shared understanding — it produces no design artifact and starts no implementation.

- **Vague idea, no plan yet → `brainstorming`.** When the user wants to turn an idea into a design or spec, use brainstorming. It owns the design → spec → plan path and the approval gate.
- **The answers live with someone else → `to-questionnaire`.** Grill the user only about what the user knows. If a question can only be answered by another person, that belongs in a questionnaire, not here.
- **Grilling settles a design that then needs building → hand off to `brainstorming`** for its architectural path (spec, then implementation plan).
- Grilling decides nothing on its own. It ends at shared understanding and waits for the user to confirm it.

Do not run brainstorming's one-question-at-a-time flow here: grilling works the whole frontier per round by design.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), find it yourself. If the developer has explicitly opted into subagents (see the subagent usage policy), you may dispatch a cheap-model sub-agent instead; otherwise look the fact up directly. Don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms you have reached a shared understanding.

## Local modifications

Imported from [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling). Changed here: added the **Scope and boundaries** section to separate it from `brainstorming` and `to-questionnaire`, and made the fact-finding step obey the subagent opt-in policy instead of always dispatching a sub-agent.
