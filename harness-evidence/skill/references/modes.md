# Operating modes

The mode controls where evidence may be gathered and which side effects are permitted. It does not replace security rules, user approval, or ownership boundaries.

## Declared mode first

Read the `mode=` field from the default `intake` snapshot before selecting automatically. Use `validate` directly only for an explicit structural check or repair workflow.

- `mode=GIT` or `mode=LOCAL`: honor the persisted declaration.
- `mode=UNDECLARED`: use the selection order below.
- Never call `mode --set` without an explicit user direction. Record that direction in a separate numbered thread message; `--by` is only an audit note, not authentication.
- If a declared boundary blocks required evidence, stop and report. Do not auto-promote `LOCAL` to `GIT`.

The helper detects declaration mismatches for thread mutations, but it cannot stop an agent from calling external tools.

## Selection order

1. Follow an explicit user-selected mode when no persisted declaration exists, or treat it as authority to change an existing declaration through the recorded workflow above.
2. Otherwise inspect the request, project instructions, newest applicable thread message, and the full working scope for a Git repository. Check the project root and known in-scope child directories; do not decide from the current directory alone or scan outside the authorized scope.
3. Select Local mode when no Git repository exists anywhere in that scope.
4. If project policy requires a current repository-host check before reading or replying, select Git mode.
5. If an in-scope repository exists, select Git mode when the task needs repository or remote-host evidence; select Local mode when the task is explicitly limited to local files and needs no repository evidence.
6. For a simple read, announce the mode only when it is undeclared, changed, mismatched, or materially limits the task. State the effective mode briefly before a mailbox mutation or before crossing a mode boundary.

## Git mode

Use Git mode for branches, commits, diffs, pull requests, reviews, Discussions, issues, CI, or repository-driven deployment.

- Read only the repository and external sources needed for the task.
- Recheck volatile remote state at the decision and append boundaries required by project policy.
- Treat local Git state, a remote host, and a deployed runtime as separate evidence sources.
- Do not infer mutation authority from the selected mode. Commit, push, comment, review, merge, deploy, and upload still require explicit user or project authorization.
- If the user says "do not push" while asking for repository inspection, remain in Git mode but keep all repository actions read-only.

## Local mode

Use Local mode when the working scope contains no Git repository, or when a repository exists but the user-authorized task is limited to local files and requires no repository evidence.

- Work only with user-scoped local files, local commands, local tests, and the local collaboration thread.
- Do not call repository-host APIs or browse repository-host pages.
- Do not fetch, pull, commit, push, create branches, open or edit pull requests, post comments or reviews, merge, deploy, or upload artifacts.
- Do not require Git, a repository, a branch, a clean worktree, or remote credentials.
- A `.git` directory does not change the mode. Ignore it unless the user explicitly requests local Git inspection.
- Report local file paths, commands, and validation results. Do not invent commit IDs, PR numbers, CI status, or deployment state.

A declared `LOCAL` mode stops the project's remote-ledger check procedure. It does **not** suspend:

- security and secret-handling rules;
- approval gates for external writes or consequential actions;
- the ban on changing another person's work without authority.

## Boundary cases

| Request or context | Mode | Authority boundary |
|---|---|---|
| "이 파일만 로컬에서 수정하고 깃은 건드리지 마" | Local | Local files and tests only |
| Local scratch directory with no repository dependency | Local | Local files and tests only |
| "PR 리뷰 확인해. 푸시는 하지 마" | Git | Remote and local reads only |
| "Discussion 확인하고 스레드에 보고해" | Git | Reading and local thread append; posting remotely needs separate authority |
| "스레드 ㄱ" in a working scope with no Git repository and no remote-check policy | Local | Local thread and local artifacts only |
| Project policy requires a current remote check before a thread reply | Git | Required reads only unless writes are separately authorized |

Repository discovery must include known in-scope child directories. A non-repository project root does not imply Local mode when a child project contains `.git`.

If a Local-mode request cannot be completed without remote evidence, stop at the boundary, report what is missing, and ask before switching. Do not silently upgrade to Git mode.
