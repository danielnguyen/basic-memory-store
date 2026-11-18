# CCP Agent Instructions

## Current instructions and objective

Read the current `danielnguyen/ai-engineering/AGENTS.md`, followed by its relevant workflow, authority, engineering and integration guidance. Use the current target repository source, task and decisions for implementation and review. For conversation-first CCP work, [CO Issue #248](https://github.com/danielnguyen/chat-orchestrator/issues/248) is the canonical product objective; it does not authorize changing other repositories. Do not fetch or clone a sibling planning repository for CCP workflow or product context.

## Repository and commit verification

Before trusting evidence or modifying code, verify the repository origin, clean isolated checkout, fetched base and current PR head. Fast-forward only when safe; never overwrite, reset or stash unrelated work. Verify task-referenced source commits and claimed branch ancestry in their owning repository. A CI-generated GitHub PR merge commit may not arrive with a normal branch fetch: when referenced, explicitly fetch `refs/pull/<number>/merge` and verify the exact SHA, parents and current PR head rather than declaring a valid synthetic merge commit missing. Do not assume such a commit is merged into `main`.

Preserve owner/thread isolation, privacy, source truth, action permissions, canonical persistence, independent review and owner-controlled merges/deployments. Stop the affected action on a genuine synchronization, integrity, access or safety failure; do not add permissions or bypass checks to proceed.

## Local containment


- Do not create new planning artifacts, execution briefs, inventory docs, cleanup plans, scripts, or Make targets unless the user explicitly asks for them or the allowed-file list names them.
- Do not introduce wave, phase, cluster, packet, pass, or requirement labels into production runtime code as architecture, symbols, trace/API fields, or provider-visible wording.
- Prefer bounded edits to existing files over adding files. If a new file or broader file scope seems necessary, stop and report why before editing.
- Do not rename historical replay, smoke, or test taxonomy unless the task explicitly asks for that cleanup.
- Treat allowed files, forbidden files, validation commands, and explicit non-goals in the user/Codex task as hard constraints.
