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
from collections.abc import Callable

import mandate

DELEGATION_REPO = "ImranAdan/mandate-delegation"
CHECK_NAME = "mandate/decision"
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

    return mandate.decide(
        delegation,
        pr=number,
        head_sha=sha,
        changed_paths=changed_paths(get, repo, number),
        checks=check_results(get, repo, sha),
        intent=intent,
        acceptance=acceptance,
    )


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


def _post_check(token: str, repo: str, sha: str, title: str, summary: str, details: str) -> None:
    body = {
        "name": CHECK_NAME,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion(title),
        "output": {"title": title, "summary": summary, "text": details},
    }
    _gh(token, f"repos/{repo}/check-runs", "--method", "POST", "--input", "-", stdin=json.dumps(body))


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
        )
    except Refused as refused:
        _post_check(token, repo, sha, "BLOCK", f"Not ready: {refused}.", "")
        print(f"BLOCK: {refused}")
        return 0
    except Exception as error:  # its text could quote private content, so only its type is shown
        print(f"Mandate couldn't finish: {type(error).__name__}")
        _post_check(token, repo, sha, "BLOCK", "Mandate couldn't finish, so nothing was decided.", "")
        return 1

    if decision is None:
        print("No open pull request has this commit, so there is nothing to decide.")
        return 0
    text = mandate.receipt_text(decision)
    _post_check(token, repo, sha, decision.outcome, text, json.dumps(mandate.receipt_record(decision), indent=2))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
