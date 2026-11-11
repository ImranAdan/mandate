"""One decision for one commit, run from a workflow on main.

Only the receipt is ever printed or posted. The intent and its tests are private,
and an Actions log on a public repo is public, so nothing else leaves this process.
"""

import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable

import anthropic

import mandate

DELEGATION_REPO = "ImranAdan/mandate-delegation"
CHECK_NAME = "mandate/decision"
CHECK_PARAMETER = "mandate%2Fdecision"
JUDGE_URL = "http://127.0.0.1:11434/v1/chat/completions"
JUDGE_REPLY_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "judgement",
        "schema": {
            "type": "object",
            "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["score", "reason"],
            "additionalProperties": False,
        },
    },
}
PER_PAGE = 100
LINKED_ISSUES = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 10) { nodes { number repository { nameWithOwner } } }
    }
  }
}
"""


class Refused(Exception):
    """A plain refusal that is safe to show on the public pull request."""


def open_prs(get: Callable, repo: str, sha: str) -> list[dict]:
    return [p for p in get(f"repos/{repo}/commits/{sha}/pulls") if p["state"] == "open" and p["head"]["sha"] == sha]


def changed_paths(get: Callable, repo: str, number: int) -> tuple[str, ...]:
    """Every changed path, with a rename's old path too. A short list is refused."""
    expected = get(f"repos/{repo}/pulls/{number}")["changed_files"]
    files = []
    page = 1
    while len(files) < expected:
        batch = get(f"repos/{repo}/pulls/{number}/files?per_page={PER_PAGE}&page={page}")
        if not batch:
            raise RuntimeError("changed files can't all be read")
        files += batch
        page += 1
    paths = {file["filename"] for file in files} | {file["previous_filename"] for file in files if "previous_filename" in file}
    return tuple(sorted(paths))


def check_results(get: Callable, repo: str, sha: str) -> tuple[mandate.CheckResult, ...]:
    """The latest check runs, each traced to the workflow run that produced it."""
    runs = get(f"repos/{repo}/actions/runs?head_sha={sha}&per_page={PER_PAGE}")
    checks = get(f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page={PER_PAGE}")
    if runs["total_count"] > PER_PAGE or checks["total_count"] > PER_PAGE:
        raise RuntimeError("too many checks to read them all")
    by_id = {run["id"]: run for run in runs["workflow_runs"]}
    results = []
    for check in checks["check_runs"]:
        match = re.search(r"/actions/runs/(\d+)/job/", check.get("details_url") or "")
        run = by_id.get(int(match[1])) if match else None
        results.append(
            mandate.CheckResult(
                name=check["name"],
                workflow=run["path"] if run else "",
                event=run["event"] if run else "",
                app=check["app"]["slug"],
                head_sha=check["head_sha"],
                status=check["status"],
                conclusion=check["conclusion"],
            )
        )
    return tuple(results)


def read_delegation(get: Callable, repo: str) -> mandate.Delegation | mandate.DelegationError:
    try:
        commit = get(f"repos/{DELEGATION_REPO}/commits/main")["sha"]
        path = f"repos/{DELEGATION_REPO}/contents/repositories/{repo.split('/', 1)[1]}.toml?ref={commit}"
        text = base64.b64decode(get(path)["content"]).decode()
        return mandate.load_delegation(text, repo, commit)
    except mandate.DelegationError as error:
        return error
    except Exception:  # a failed read must never look like usable settings
        return mandate.DelegationError("the settings can't be read")


def run_acceptance(intent: mandate.Intent, base_url: str) -> mandate.Acceptance:
    """Run the private tests against the app. No token reaches them, and their output is dropped."""
    with tempfile.TemporaryDirectory() as folder:
        tests = pathlib.Path(folder, "test_acceptance.py")
        tests.write_text(intent.tests)
        report = pathlib.Path(folder, "report.xml")
        subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest",
             f"--rootdir={folder}", f"--junitxml={report}", str(tests)],
            cwd=folder,
            env={"PATH": os.environ["PATH"], "BASE_URL": base_url},
            capture_output=True,
            timeout=300,
        )  # fmt: skip
        return mandate.read_acceptance(report.read_text() if report.exists() else "", intent)


def evaluate(
    repo: str,
    sha: str,
    get: Callable,
    get_private: Callable,
    linked_issues: Callable,
    run_tests: Callable,
    read_diff: Callable,
    ask_judge: Callable,
) -> mandate.Decision | None:
    """The decision for one commit, or None if no open pull request has it."""
    prs = open_prs(get, repo, sha)
    if not prs:
        return None
    if len(prs) > 1:
        raise Refused("more than one pull request shares this commit")
    number = prs[0]["number"]

    delegation = read_delegation(get, repo)
    intent: mandate.Intent | mandate.IntentUnbound | mandate.IntentError
    intent = mandate.IntentError("the settings can't be used")
    acceptance = None
    if isinstance(delegation, mandate.Delegation):
        try:
            intents = mandate.read_intents(get_private, delegation.intent_repo, repo.split("/", 1)[1])
            intent = mandate.find_intent(linked_issues(number), intents)
            acceptance = run_tests(intent)
        except (mandate.IntentError, mandate.IntentUnbound) as error:
            intent = error

    paths = changed_paths(get, repo, number)
    checks = check_results(get, repo, sha)

    def decide(score: object = None, response: str | None = None, refusals: int = 0) -> mandate.Decision:
        return mandate.decide(delegation, number, sha, paths, checks, intent, acceptance, score, response, refusals)

    decision = decide()
    if [reason.code for reason in decision.reasons] != ["JUDGE_SCORE_MISSING"] or delegation.judge_model == mandate.NO_JUDGE:
        return decision
    records = decision_history(get, repo, number)
    refusals = prior_refusals(records, sha)
    if refusals >= mandate.REFUSAL_LIMIT:
        return decide(refusals=refusals)
    earlier = earlier_score(records, sha, delegation.commit, intent.commit, delegation.judge_threshold)
    if earlier:
        return decide(earlier["judge_score"], earlier.get("judge_response"), refusals)
    return decide(*_judged(delegation, intent, number, read_diff, ask_judge), refusals)


def decision_history(get: Callable, repo: str, number: int) -> list[dict]:
    """Earlier receipts for this pull request, newest first within each commit.

    ponytail: any workflow on a branch can post a check under this name and app, so
    this history can be forged until the check moves to Mandate's own App. It only
    decides a dry run for now, and stage 3 must filter on that App instead.
    """
    records = []
    for commit in get(f"repos/{repo}/pulls/{number}/commits?per_page={PER_PAGE}"):
        path = f"repos/{repo}/commits/{commit['sha']}/check-runs?check_name={CHECK_PARAMETER}&filter=all&per_page={PER_PAGE}"
        for run in get(path)["check_runs"]:
            record = mandate.read_receipt(run["output"].get("text")) if run["app"]["slug"] == mandate.GITHUB_ACTIONS else None
            if record and record.get("pr") == number:
                records.append(record)
    return records


def prior_refusals(records: list[dict], sha: str) -> int:
    """How many other commits of this pull request the Judge refused."""
    return len(
        {
            record.get("commit")
            for record in records
            if record.get("commit") != sha
            and any(reason.get("code") in mandate.REFUSAL_CODES for reason in record.get("reasons", []))
        }
    )


def earlier_score(records: list[dict], sha: str, delegation_commit: str, intent_commit: str, threshold: float) -> dict | None:
    """The newest refusal for this commit under these settings and intent, so a rerun can't re-roll it.

    The settings commit changes whenever the model does, and receipts never name the model.

    A passing score is never reused, because a workflow on any branch can forge one
    in this history. The Judge is asked again instead.
    """
    for record in records:
        score = record.get("judge_score")
        if (
            record.get("commit") == sha
            and record.get("delegation_commit") == delegation_commit
            and record.get("intent_commit") == intent_commit
            and type(score) in (int, float)
            and score < threshold
        ):
            return record
    return None


def _judged(
    delegation: mandate.Delegation, intent: mandate.Intent, number: int, read_diff: Callable, ask_judge: Callable
) -> tuple[object, str]:
    """The score and response. Any failure gives no score, so nothing can pass by it."""
    try:
        diff = read_diff(number)
    except Exception:
        return None, "The change couldn't be read."
    if len(diff) > mandate.MAX_DIFF_CHARS:
        return None, "The change is too large to judge."
    try:
        reply = ask_judge(delegation.judge_model, mandate.judge_prompt(intent, diff))
    except Exception as error:  # its text could quote private content, so only its kind is shown
        status = f" {error.status_code}" if isinstance(error, anthropic.APIStatusError) else ""
        return None, f"The model gave no answer ({type(error).__name__}{status})."
    return mandate.read_score(reply), reply


def ask_local_model(model: str, prompt: str) -> str:
    """Ask a local model through its OpenAI-compatible API, downloading it first if needed."""
    subprocess.run(["docker", "exec", "ollama", "ollama", "pull", model], capture_output=True, check=True, timeout=600)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": mandate.JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
        "response_format": JUDGE_REPLY_FORMAT,
        "max_tokens": 300,
    }
    request = urllib.request.Request(JUDGE_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.load(response)["choices"][0]["message"]["content"]


def ask_claude(model: str, prompt: str, key: str) -> str:
    """Ask a Claude model through Anthropic's API. Anything but a finished answer raises, so it gives no score."""
    if not key:  # without it the SDK would look for other credentials, possibly another account's
        raise RuntimeError("no ANTHROPIC_API_KEY")
    response = anthropic.Anthropic(api_key=key).messages.create(
        model=model,
        max_tokens=16000,
        system=mandate.JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": JUDGE_REPLY_FORMAT["json_schema"]["schema"]}},
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(response.stop_reason)
    return next(block.text for block in response.content if block.type == "text")


def asker(key: str | None) -> Callable[[str, str], str]:
    """The adapter for a model name: claude-* goes to Anthropic, anything else to the local server."""
    return lambda model, prompt: ask_claude(model, prompt, key) if model.startswith("claude-") else ask_local_model(model, prompt)


def should_merge(decision: mandate.Decision) -> bool:
    """Only an AUTHORIZE outside a dry run merges."""
    return decision.outcome == "AUTHORIZE" and not decision.dry_run


def _merge(token: str, repo: str, number: int, sha: str) -> str:
    """Merge exactly this commit and return the merge commit. GitHub refuses if the branch has moved on since."""
    return _gh(token, f"repos/{repo}/pulls/{number}/merge", "--method", "PUT", "-f", f"sha={sha}", "-f", "merge_method=merge")["sha"]


def _close_issue(token: str, repo: str, issue: int, number: int, merged_as: str) -> None:
    """Say on the issue what closed it, then close it. A closing keyword doesn't fire for this token's merges."""
    body = f"Done in #{number}, which Mandate merged as {merged_as[:7]}."
    _gh(token, f"repos/{repo}/issues/{issue}/comments", "--method", "POST", "-f", f"body={body}")
    _gh(token, f"repos/{repo}/issues/{issue}", "--method", "PATCH", "-f", "state=closed", "-f", "state_reason=completed")


def own_comment(comments: list[dict]) -> int | None:
    """Mandate's comment on the pull request, if it has one, so each decision updates it."""
    for comment in comments:
        if comment["user"]["login"] == f"{mandate.GITHUB_ACTIONS}[bot]" and comment["body"].startswith(mandate.COMMENT_MARKER):
            return comment["id"]
    return None


def _post_comment(token: str, repo: str, number: int, body: str) -> None:
    pages = _gh(token, f"repos/{repo}/issues/{number}/comments?per_page={PER_PAGE}", "--paginate", "--slurp")
    found = own_comment([comment for page in pages for comment in page])
    if found:
        _gh(token, f"repos/{repo}/issues/comments/{found}", "--method", "PATCH", "-f", f"body={body}")
    else:
        _gh(token, f"repos/{repo}/issues/{number}/comments", "--method", "POST", "-f", f"body={body}")


def conclusion(outcome: str) -> str:
    """Only AUTHORIZE passes, because a required check also passes on neutral or skipped."""
    return "success" if outcome == "AUTHORIZE" else "failure"


def _gh(token: str, *arguments: str, stdin: str | None = None) -> object:
    done = subprocess.run(
        ["gh", "api", *arguments],
        env={**os.environ, "GH_TOKEN": token},
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _post_check(token: str, repo: str, sha: str, outcome: str, title: str, summary: str, details: str) -> None:
    body = {
        "name": CHECK_NAME,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion(outcome),
        "output": {"title": title, "summary": summary, "text": details},
    }
    _gh(token, f"repos/{repo}/check-runs", "--method", "POST", "--input", "-", stdin=json.dumps(body))


def _read_diff(token: str, repo: str, number: int) -> str:
    done = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.diff", f"repos/{repo}/pulls/{number}"],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _linked_issues(token: str, repo: str, number: int) -> tuple[int, ...]:
    owner, name = repo.split("/", 1)
    answer = _gh(
        token, "graphql", "-f", f"query={LINKED_ISSUES}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"
    )
    nodes = answer["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    return tuple(node["number"] for node in nodes if node["repository"]["nameWithOwner"] == repo)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["HEAD_SHA"]
    base_url = os.environ["BASE_URL"]
    token = os.environ.pop("GH_TOKEN")
    private_token = os.environ.pop("INTENT_TOKEN")
    claude_key = os.environ.pop("ANTHROPIC_API_KEY", None)

    def get(path: str) -> object:
        return _gh(token, path)

    def get_private(path: str) -> object:
        return _gh(private_token, path)

    try:
        decision = evaluate(
            repo,
            sha,
            get,
            get_private,
            lambda number: _linked_issues(token, repo, number),
            lambda intent: run_acceptance(intent, base_url),
            lambda number: _read_diff(token, repo, number),
            asker(claude_key),
        )
    except Refused as refused:
        _post_check(token, repo, sha, "BLOCK", f"Not ready: {refused}.", f"## ❌ Not ready\n\n{refused}.\n", "")
        print(f"BLOCK: {refused}")
        return 0
    except Exception as error:  # its text could quote private content, so only its type is shown
        print(f"Mandate couldn't finish: {type(error).__name__}")
        title = "Mandate couldn't finish, so nothing was decided."
        _post_check(token, repo, sha, "BLOCK", title, f"## ❌ Not ready\n\n{title}\n", "")
        return 1

    if decision is None:
        print("No open pull request has this commit, so there is nothing to decide.")
        return 0
    _post_check(
        token,
        repo,
        sha,
        decision.outcome,
        mandate.message(decision),
        mandate.check_summary(decision),
        mandate.check_details(decision),
    )
    print(mandate.receipt_text(decision))
    merged_as = closed_issue = None
    failed = False
    if should_merge(decision):
        try:
            merged_as = _merge(token, repo, decision.pr, sha)
            print(f"Merged PR #{decision.pr} at {sha[:7]} as {merged_as[:7]}")
            _close_issue(token, repo, decision.intent.issue, decision.pr, merged_as)
            closed_issue = decision.intent.issue
            print(f"Closed issue #{closed_issue}")
        except Exception as error:  # only its type, as above
            print(f"Authorized, but the merge or closing the issue failed: {type(error).__name__}")
            failed = True
    try:
        _post_comment(token, repo, decision.pr, mandate.pr_comment(decision, merged_as, closed_issue))
    except Exception as error:
        print(f"The comment couldn't be posted: {type(error).__name__}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
