# CCP Agent Instructions

Before any work, synchronize this repository and `../projects` from `origin`, update the declared base branches by fast-forward only, and verify every commit SHA named by the task in the repository where it is claimed to exist.

Stop before editing when synchronization fails, local state is unrelated or unsafe, a referenced SHA is unavailable, or a SHA claimed to be merged is not reachable from the claimed remote branch. Never continue from stale local state.

Then read and follow:

- `../projects/process/repository-sync-preflight.md`
- `../projects/AGENTS.md`

If either canonical file cannot be loaded after synchronization, stop and report the missing governance source.
