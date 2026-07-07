# CCP Agent Instructions

Before any work, synchronize this repository and `../projects` from `origin`, update the declared base branches by fast-forward only, and verify every commit SHA named by the task in the repository where it is claimed to exist.

Stop before editing when synchronization fails, local state is unrelated or unsafe, a referenced SHA is unavailable, or a SHA claimed to be merged is not reachable from the claimed remote branch. Never continue from stale local state.

Then read and follow:

- `../projects/process/repository-sync-preflight.md`
- `../projects/AGENTS.md`

`../projects/AGENTS.md` is the canonical CCP workflow source. Its Minimal CCP Execution Protocol is binding in this repository.

Local containment rules:

- Do not create new planning artifacts, execution briefs, inventory docs, cleanup plans, scripts, or Make targets unless the user explicitly asks for them or the allowed-file list names them.
- Do not introduce wave, phase, cluster, packet, pass, or requirement labels into production runtime code as architecture, symbols, trace/API fields, or provider-visible wording.
- Prefer bounded edits to existing files over adding files. If a new file or broader file scope seems necessary, stop and report why before editing.
- Do not rename historical replay, smoke, or test taxonomy unless the task explicitly asks for that cleanup.
- Treat allowed files, forbidden files, validation commands, and explicit non-goals in the user/Codex task as hard constraints.

If either canonical file cannot be loaded after synchronization, stop and report the missing governance source.
