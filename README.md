# mandate

A small merge authority: it decides whether an agent's pull request may merge.

See the [design](docs/design.md) for how the whole flow fits together, with diagrams.

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

Code blocks are free text too. `load_intent` refuses an intent that breaks this, including one naming a test that doesn't exist, naming no tests at all, or with a code block that never closes.

## Finding the intent

`read_intents` reads every intent for an app at one commit of the private repo, and `find_intent` picks the one that claims the issue the PR links.

| The PR links | Result |
|---|---|
| One issue an intent claims | Judged against that intent |
| No issue, several, or an unclaimed one | `BLOCK`, which the agent can fix |
| An issue two intents claim, or any bad intent file | `ESCALATE`, without quoting the intent, because the message is public |

A failed read never counts as having no intents. The token for that repo is short-lived and read-only, and the key behind it stays in an environment only `main` can use.

## Running a decision

`run.py` runs one decision from a workflow on `main`, and it's the only part that talks to GitHub. It gathers the PR's changed files, CI results and linked issue, runs the acceptance tests, decides, and posts the receipt as the `mandate/decision` check. That check passes only on `AUTHORIZE`, because a required check also passes on neutral or skipped.

Only the receipt is ever printed or posted. An Actions log on a public repo is public, so the acceptance tests' output is dropped, and if a run fails, only the error's type is shown. Those tests get no token. Anything that can't be read in full, such as a short list of changed files, stops the decision rather than being guessed at.

## The Judge

When everything else has passed and `judge_model` isn't `none`, `run.py` asks a local model through Ollama's OpenAI-compatible API. It sends the intent, with test names removed, and the PR's diff. The model's reply shows on the check for debugging, and only its score counts. A failed call, an unusable reply, or a diff over `MAX_DIFF_CHARS` gives no score, so nothing can pass by it. Only this one kind of model is supported so far.

## Known limits

- **Reserved paths:** these are found by path alone, so any `.github/` change comes to the owner, and SQL inside application code isn't seen.
- **Receipts:** a plain record resists tampering less than a hash-chained ledger.

## Test it

```bash
uv run pytest
```
