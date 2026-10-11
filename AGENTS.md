# Agent Instructions

This file is the working contract for AI agents and contributors in this repository. It says how to work and where to look; facts about the bridge live in [`README.md`](README.md) (what it is and how to run it) and [`docs/`](docs/README.md) (how it works inside).

## Sources Of Truth

- `docs/` describes how the bridge works today. Start at [`docs/README.md`](docs/README.md).
- The Agora bridge protocol is defined by Agora: [`docs/architecture/bridges.md`](https://github.com/vsem-azamat/agora/blob/main/docs/architecture/bridges.md) in `vsem-azamat/agora`. This bridge follows it; it does not extend it on its own.
- Code is the source of truth for how something is implemented. If code and docs disagree, reconcile them in the same change.

## Current State Only

- Docs describe the bridge as it is now. No history, decision logs, plans, roadmaps or status notes.
- A behavior change updates the docs and tests in the same PR as the code.
- Rationale belongs in the PR description; deferred work belongs in an issue.
- Do not commit scratch files, agent plans, handoff notes, logs, session files or temporary output.

## Public Repository Hygiene

This repository is public. Before every commit and PR, check that nothing private goes in:

- no real hostnames, IP addresses, network names, home-directory paths or usernames of any machine;
- no real Telegram usernames, user ids, chat ids, phone numbers or message contents;
- no names of private projects, companies or clients the bridge is used with;
- no emails, tokens, API ids or hashes, session strings or `.env` contents;
- examples use neutral placeholders such as `-1001234567890`, `Ada`, `~/src/example-app`.

## Order Of Work

Within one branch and one pull request:

1. Inspect the current checkout and read the relevant docs.
2. Write a failing test that enforces the behavior and watch it fail for the expected reason.
3. Implement the smallest change that makes it pass, then clean up.
4. Update `README.md` or `docs/` when what users or contributors need to know changes.
5. Run the checks before claiming success or pushing:

   ```sh
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   ```

Tests never reach Telegram or an MCP server: they use fakes. Do not log in to Telegram or send to real chats from tests or while developing.

## Quality Bar

- Fix root causes. Do not ship code you cannot explain.
- State uncertainty and risks explicitly.
- No abstraction until two real call sites need it.
- Do not mix unrelated cleanup into a change.
- Fix lint findings instead of silencing them. A `# noqa: <code>` comment gives its reason on the same line.
- Logs carry ids and counts, never message text, tokens or session data.

## Code Review

Every pull request gets an independent review before it is merged: a reviewer who did not write the change (another agent or a person).

- At most 3 review rounds; a 4th only when the 3rd left a blocker.
- Sort the findings:
  - **fix** defects: wrong behavior, security problems, a broken contract, code that disagrees with the docs or tests;
  - **file an issue** for what is outside the task;
  - **drop** matters of taste and "what if" concerns without a concrete input that triggers them.
- Deferred and dropped findings get one line each in the PR description.

The reviewer checks for:

- typed contracts at the boundaries (the hub's JSON lines, the Telegram backends);
- no magic values: named constants or configuration;
- consistency with the existing code;
- docs, tests and code that agree;
- tests that actually catch the scenario they name;
- errors that are not swallowed.

## Git And Pull Requests

- Branch from `main`; one topic per branch; conventional commit messages (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`).
- Every change lands through a pull request with green CI and is squash-merged. The PR description says what changed, why, and how it was verified.
- Releases are tags on `main` and are created by the maintainer only.
- Do not add AI attribution lines or trailers to commits or PRs.

## Instruction Files

`AGENTS.md` is the only agent instruction file. Do not add `CLAUDE.md` or tool-specific copies; CI rejects them.
