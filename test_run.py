import base64

import pytest

import mandate
import run

REPO = "o/app"
SHA = "a" * 40
DELEGATION_COMMIT = "b" * 40
INTENT_COMMIT = "c" * 40
DELEGATION = """
repository = "o/app"
dry_run = true
judge_model = "none"
intent_repo = "o/intents"

[proofs.tests]
check = "test"
workflow = ".github/workflows/ci.yml"
"""
INTENT = """# A private title

Issue: #2

## Feature

- A private requirement. Test: `test_one`
"""
TESTS = "def test_one():\n    pass\n"


def encoded(text):
    return {"content": base64.b64encode(text.encode()).decode()}


def pr(number=7, sha=SHA, state="open", changed_files=2):
    return {"number": number, "state": state, "head": {"sha": sha}, "changed_files": changed_files}


class Fake:
    """GitHub as these functions see it."""

    def __init__(self, prs=None, files=None, check_conclusion="success", delegation=DELEGATION):
        self.prs = [pr()] if prs is None else prs
        self.files = files or [{"filename": "tasks_api.py"}, {"filename": "README.md"}]
        self.check_conclusion = check_conclusion
        self.delegation = delegation

    def get(self, path):
        pages = {
            f"repos/{REPO}/commits/{SHA}/pulls": self.prs,
            f"repos/{REPO}/pulls/7": pr(changed_files=len(self.files)),
            f"repos/{REPO}/pulls/7/files?per_page=100&page=1": self.files,
            f"repos/{REPO}/pulls/7/files?per_page=100&page=2": [],
            f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100": {
                "total_count": 1,
                "workflow_runs": [{"id": 11, "path": ".github/workflows/ci.yml", "event": "pull_request"}],
            },
            f"repos/{REPO}/commits/{SHA}/check-runs?filter=latest&per_page=100": {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "test",
                        "app": {"slug": "github-actions"},
                        "head_sha": SHA,
                        "status": "completed",
                        "conclusion": self.check_conclusion,
                        "details_url": f"https://github.com/{REPO}/actions/runs/11/job/5",
                    },
                    {
                        "name": "CodeRabbit",
                        "app": {"slug": "coderabbitai"},
                        "head_sha": SHA,
                        "status": "completed",
                        "conclusion": "neutral",
                        "details_url": "https://example.com",
                    },
                ],
            },
            "repos/ImranAdan/mandate-delegation/commits/main": {"sha": DELEGATION_COMMIT},
            f"repos/ImranAdan/mandate-delegation/contents/repositories/app.toml?ref={DELEGATION_COMMIT}": encoded(self.delegation),
        }
        return pages[path]

    def get_private(self, path):
        pages = {
            "repos/o/intents/commits/main": {"sha": INTENT_COMMIT},
            f"repos/o/intents/contents/app?ref={INTENT_COMMIT}": [{"name": "a.md"}, {"name": "test_a.py"}],
            f"repos/o/intents/contents/app/a.md?ref={INTENT_COMMIT}": encoded(INTENT),
            f"repos/o/intents/contents/app/test_a.py?ref={INTENT_COMMIT}": encoded(TESTS),
        }
        return pages[path]


def evaluate(fake=None, linked=(2,), ran=None):
    fake = fake or Fake()
    ran = [] if ran is None else ran

    def run_tests(intent):
        ran.append(intent.issue)
        return mandate.Acceptance(1, 1)

    return run.evaluate(REPO, SHA, fake.get, fake.get_private, lambda number: linked, run_tests)


def test_a_linked_pr_with_passing_proofs_waits_for_the_score():
    decision = evaluate()
    assert decision.outcome == "VERIFY"
    assert [reason.code for reason in decision.reasons] == ["JUDGE_SCORE_MISSING"]
    assert decision.pr == 7
    assert decision.head_sha == SHA
    record = mandate.receipt_record(decision)
    assert record["delegation_commit"] == DELEGATION_COMMIT
    assert record["intent_issue"] == 2
    assert record["intent_commit"] == INTENT_COMMIT


def test_the_acceptance_tests_run_only_for_a_linked_pr():
    ran = []
    evaluate(ran=ran)
    assert ran == [2]

    ran = []
    decision = evaluate(linked=(), ran=ran)
    assert ran == []
    assert decision.outcome == "BLOCK"
    assert mandate.message(decision) == "Not ready: link exactly one issue."


def test_a_failed_ci_check_blocks():
    decision = evaluate(Fake(check_conclusion="failure"))
    assert decision.outcome == "BLOCK"
    assert [reason.code for reason in decision.reasons] == ["PROOF_FAILED"]


def test_no_open_pr_means_there_is_nothing_to_decide():
    assert evaluate(Fake(prs=[])) is None
    assert evaluate(Fake(prs=[pr(state="closed")])) is None
    assert evaluate(Fake(prs=[pr(sha="d" * 40)])) is None


def test_two_prs_on_one_commit_are_refused():
    with pytest.raises(run.Refused, match="more than one pull request shares this commit"):
        evaluate(Fake(prs=[pr(), pr(number=8)]))


def test_a_rename_counts_both_paths():
    fake = Fake(files=[{"filename": "moved.yml", "previous_filename": ".github/workflows/ci.yml"}])
    decision = evaluate(fake)
    assert decision.outcome == "ESCALATE"
    assert decision.reasons[0].code == "RESERVED:automation"


def test_a_changed_files_list_that_runs_short_is_refused():
    fake = Fake()
    real_get = fake.get

    def get(path):
        if path == f"repos/{REPO}/pulls/7":
            return pr(changed_files=5)
        return real_get(path)

    with pytest.raises(RuntimeError, match="changed files can't all be read"):
        run.changed_paths(get, REPO, 7)


def test_an_unreadable_delegation_comes_to_the_owner():
    fake = Fake(delegation="not toml =")
    decision = evaluate(fake)
    assert decision.outcome == "ESCALATE"
    assert decision.reasons[0].code == "DELEGATION_INVALID"


def test_a_check_with_no_known_run_has_no_source():
    fake = Fake()
    real_get = fake.get

    def get(path):
        if "actions/runs?" in path:
            return {"total_count": 0, "workflow_runs": []}
        return real_get(path)

    [result, _] = run.check_results(get, REPO, SHA)
    assert (result.name, result.workflow, result.event) == ("test", "", "")


def test_too_many_checks_are_refused_rather_than_truncated():
    fake = Fake()
    real_get = fake.get

    def get(path):
        answer = real_get(path)
        return answer | {"total_count": 101} if "check-runs" in path else answer

    with pytest.raises(RuntimeError, match="too many checks"):
        run.check_results(get, REPO, SHA)


def test_acceptance_tests_run_with_no_token_and_print_nothing(capfd, monkeypatch, tmp_path):
    monkeypatch.setenv("INTENT_TOKEN", "a-secret-token")
    monkeypatch.setenv("GH_TOKEN", "another-secret-token")
    tests = """
import os

def test_env_is_clean():
    assert os.environ["BASE_URL"] == "http://app:8000"
    assert "INTENT_TOKEN" not in os.environ
    assert "GH_TOKEN" not in os.environ

def test_private_failure():
    assert "a private expected value" == "nope"
"""
    intent = mandate.load_intent(
        "# T\n\nIssue: #2\n\n- A. Test: `test_env_is_clean`\n- B. Test: `test_private_failure`\n", tests
    )
    result = run.run_acceptance(intent, "http://app:8000")
    assert result == mandate.Acceptance(1, 2)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_the_check_only_passes_on_authorize():
    assert run.conclusion("AUTHORIZE") == "success"
    for outcome in ("BLOCK", "ESCALATE", "VERIFY"):
        assert run.conclusion(outcome) == "failure"
