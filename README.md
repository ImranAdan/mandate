# mandate

A small merge authority: it decides whether an agent's pull request may merge.

`mandate.py` holds the logic only. The settings it reads live in the owner's `mandate-delegation` repo, at `repositories/<repo>.toml`, so a change of rules never touches this repo.

## How a decision works

The outcome is the worst reason found. `AUTHORIZE` means there are no reasons at all.

| Outcome | Means | Example |
|---|---|---|
| `BLOCK` | Something is wrong and must be fixed | A proof failed, or `JudgeScore` is below the threshold |
| `ESCALATE` | It needs the owner | A reserved path changed, or the settings are invalid |
| `VERIFY` | Proof is still missing | CI is running, or there's no `JudgeScore` yet |
| `AUTHORIZE` | It may merge | Every proof passed and the score is high enough |

PR text is never an input, and a `JudgeScore` can refuse a merge but can't authorize one.

## Settings

| Setting | Meaning | If missing |
|---|---|---|
| `repository` | The repo these settings are for | Refused |
| `dry_run` | When true, record decisions and merge nothing | `true`, with a warning |
| `judge_model` | The Judge's model; `none` skips it and forces `dry_run` on, with a warning | `none`, with a warning |
| `judge_threshold` | The minimum `JudgeScore`, from 0 to 1 | `0.8` |
| `judge_reads_pr_text` | Whether the Judge also reads the PR's title and description | `false` |
| `intent_repo` | The private repo holding intents | Refused |
| `proofs` | The checks that must pass: each names a `check` and its `workflow` | Refused |

Any other setting makes the whole file invalid, and then every pull request comes to the owner.

## Intent format

An intent has a title, one `Issue:` line, and any sections. Everything is free text except the bullets: each one names a test or says `Judge only`, apart from those under `Out of scope`.

```markdown
# Filter tasks by done state

Issue: #2

## Feature

`GET /tasks` accepts an optional `done` filter.

- `done=false` returns only open tasks. Test: `test_open_only`

## Security

- The code contains no secrets. Judge only

## Out of scope

Search, sorting and paging.
```

`load_intent` refuses an intent that breaks this, including one naming a test that doesn't exist or naming no tests at all.

## Known limits

- **Reserved paths:** these are found by path alone, so any `.github/` change comes to the owner, and SQL inside application code isn't seen.
- **Receipts:** a plain record resists tampering less than a hash-chained ledger.

## Test it

```bash
uv run pytest
```
