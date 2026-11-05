# Mandate design

Mandate decides whether an agent's pull request may merge. It runs on a small app, [`tasks-api`](https://github.com/ImranAdan/tasks-api), so the decision can be developed and proved on something simple.

**Status:** the flow runs end to end on real pull requests, in dry run, with no judge yet. So far Mandate has only refused or waited; nothing has merged through it. See [What's built and what's next](#whats-built-and-whats-next).

## The flow

Intent → Issue → PR → Evidence → Decision → Merge → Outcome

Mandate's job starts at the pull request. Writing the intent and turning it into an issue belong to the product owner.

## The parts

```mermaid
flowchart LR
    owner(["Product owner"])
    agent(["Implementing agent"])

    subgraph private["Private"]
        intent["mandate-intent<br/>intents and acceptance tests"]
    end

    subgraph public["Public"]
        delegation["mandate-delegation<br/>rules and settings"]
        engine["mandate<br/>decision logic"]
        app["tasks-api<br/>the app and its workflow"]
    end

    owner -->|writes| intent
    owner -->|merges| delegation
    owner -->|opens issues in| app
    agent -->|opens pull requests in| app
    app -->|pins one commit of| engine
    app -->|reads| delegation
    app -->|reads with a read-only token| intent
```

| Repo | Holds | Who changes it |
|---|---|---|
| `mandate-delegation` | Rules and settings, one file per app | Only the owner merges |
| `mandate` | The decision logic | Agents, but it has no effect until an app moves its pin |
| `mandate-intent` | Intents and acceptance tests, one folder per app | The product owner. Agents are meant to have no access, but that isn't enforced until stage 3 |
| `tasks-api` | The app, its tests and its workflow | The implementing agent. Agent changes to `.github/` come to the owner |

## One pull request, end to end

```mermaid
sequenceDiagram
    actor PO as Product owner
    participant IR as mandate-intent
    participant Issue as Issue
    actor Agent as Implementing agent
    participant PR as Pull request
    participant CI
    participant WF as Mandate workflow
    participant Box as Sealed app
    participant Del as mandate-delegation

    PO->>IR: Write the intent and its tests
    PO->>Issue: Say what to build
    Agent->>Issue: Read the issue, and nothing else
    Agent->>PR: Open a PR that links the issue
    PR->>CI: Run the agent's own tests
    CI-->>WF: Finished, so start main's copy of the workflow
    WF->>Box: Build the PR's app and seal it off
    Note over WF,Box: No token exists yet, and the tests aren't on the machine
    WF->>Del: Read the rules
    WF->>IR: Read the intent and tests with a read-only token
    WF->>Box: Run the hidden tests over HTTP
    Box-->>WF: Responses
    WF->>PR: Post the decision as the mandate/decision check
    Note over WF,PR: In a dry run, nothing merges
```

The later stages add two steps between the hidden tests and the check: the judge scores the change, and after `AUTHORIZE` Mandate merges and reruns the tests on the merge commit.

## How a decision is made

```mermaid
flowchart TD
    settings{"Are the settings usable?"}
    settings -->|No| escalate1["ESCALATE"]
    settings -->|Yes| gather["Gather every reason:<br/>CI proof, reserved paths,<br/>linked intent, hidden tests"]
    gather --> any{"Any reasons?"}
    any -->|Yes| worst["The worst reason wins:<br/>BLOCK, then ESCALATE, then VERIFY"]
    any -->|No| judge{"JudgeScore?"}
    judge -->|Missing| verify["VERIFY"]
    judge -->|Below the threshold| block["BLOCK"]
    judge -->|At or above it| authorize["AUTHORIZE"]
    authorize --> dry{"dry_run?"}
    dry -->|Yes| record["Record it and merge nothing"]
    dry -->|No| merge["Merge the exact commit (not built yet)"]
```

| Finding | Outcome | Who acts next |
|---|---|---|
| A CI proof failed, or came from the wrong workflow, event or app | `BLOCK` | Agent |
| A hidden test failed | `BLOCK` | Agent |
| The PR doesn't link exactly one issue that an intent claims | `BLOCK` | Agent |
| `JudgeScore` is below `judge_threshold` | `BLOCK` | Agent |
| A reserved path changed: `.github/`, agent instructions or migrations | `ESCALATE` | Owner |
| The settings or the intent can't be used | `ESCALATE` | Owner |
| The PR changes nothing | `ESCALATE` | Owner |
| A CI proof, the hidden tests or the `JudgeScore` is missing, or CI is still running | `VERIFY` | Whatever is pending |
| None of these | `AUTHORIZE` | Nobody |

`JudgeScore` can refuse a merge but never authorize one, and it only counts once everything else has passed. PR text is never an input.

## Keeping secrets and tests away from the agent

The agent's code has to run for the hidden tests to judge it, on the same machine that later holds a token. So the order matters:

1. **Build and seal:** the PR's app is built from `main`'s Dockerfile, then runs in a container with no secrets, read-only files, and no internet. The workflow stops if the app can reach the internet.
2. **Then get the token:** a short-lived, read-only token for `mandate-intent` is created only after that.
3. **Then run the tests:** they run outside the container, against the app over HTTP, so PR code can't change them or read them. They get no token.
4. **Share only the receipt:** Actions logs on a public repo are public, so test output is dropped, there are no artifacts, and a failed run shows only the error's type.

The App's key is an environment secret that only `main` can use, because a workflow on another branch could otherwise try to read a normal repo secret. The App can read only `mandate-intent`.

## Intents

One file, with a title, one `Issue:` line and any sections. Each bullet names a test or says `Judge only`, apart from those under `Out of scope`, and at least one must name a test. Everything else is free text.

```markdown
# Archive old notes

Issue: #2

## Feature

Old notes can be archived.

- Archiving hides a note from the list. Test: `test_archive_hides_a_note`

## Security

- The code contains no secrets. Judge only

## Out of scope

Search and tags.
```

The issue gives the agent the requirements, but never the tests.

## What it has done on real pull requests

| Pull request | What it tried | Result |
|---|---|---|
| [`tasks-api` #4](https://github.com/ImranAdan/tasks-api/pull/4) | Links no issue | `BLOCK`: link exactly one issue |
| [`tasks-api` #5](https://github.com/ImranAdan/tasks-api/pull/5) | Edits the workflow so it skips the decision | `BLOCK`: 3 of 5 acceptance tests failed. The edit didn't run, and the reserved path was recorded |
| [`tasks-api` #7](https://github.com/ImranAdan/tasks-api/pull/7) | A fresh agent builds the feature from the issue alone | `VERIFY`: JudgeScore missing, after passing CI and the hidden tests |

## What's built and what's next

| Stage | Settings | Proves | Status |
|---|---|---|---|
| **1. Flow only** | `judge_model = "none"`, dry run | PR, CI, decision and receipt work on real PRs | Done |
| **2. Judge scoring** | A real model, dry run | Scores match what we'd want, and 0.8 is sensible | Next |
| **3. Real merge** | A real model, `dry_run = false` | Mandate merges, and the hidden tests pass again | Not started |

The finish line is one real agent PR through stage 3 with no owner action during that PR.

## Known limits

- **Agent identity:** agents still use the owner's login, so only an instruction keeps them out of `mandate-intent` and off `main`. Stage 3 adds their own identity and a ruleset. Until then, the protections above assume no agent pushes to `main`.
- **Reserved paths:** they're found by path alone, so SQL inside application code isn't seen. The judge has to catch that.
- **Receipts:** a receipt is a plain record in a check on the PR. It resists tampering less than a hash-chained ledger, and it won't last forever.
- **What a pass means:** passing proves what the tests and judge cover. A bug neither sees can still merge, and `LIFECYCLE_OK` says nothing about runtime health.
- **Red checks:** everything except `AUTHORIZE` shows as a failed check, even when it's only waiting, because GitHub treats [`neutral` and `skipped` as passing](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) for a required check.
- **Old base commits:** the engine doesn't prove which base commit CI tested. Stage 3 relies on GitHub's [up-to-date requirement](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) for that.
- **Review:** most of this code has had no independent review.

## Sources

- [GitHub: about protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [GitHub: deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
- [GitHub: `workflow_run` event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)
