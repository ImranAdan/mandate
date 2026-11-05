import base64
import json

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
        self.pr_commits = [{"sha": SHA}]
        self.decisions = {}

    def get(self, path):
        for sha, runs in self.decisions.items():
            if path == f"repos/{REPO}/commits/{sha}/check-runs?check_name=mandate%2Fdecision&filter=all&per_page=100":
                return {"check_runs": runs}
        if "check_name=" in path:
            return {"check_runs": []}
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
            f"repos/{REPO}/pulls/7/commits?per_page=100": self.pr_commits,
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


def no_judge(model, prompt):
    raise AssertionError("the Judge shouldn't be asked")


def evaluate(fake=None, linked=(2,), ran=None):
    fake = fake or Fake()
    ran = [] if ran is None else ran

    def run_tests(intent):
        ran.append(intent.issue)
        return mandate.Acceptance(1, 1)

    return run.evaluate(REPO, SHA, fake.get, fake.get_private, lambda number: linked, run_tests, lambda number: "", no_judge)


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


def test_a_failed_delegation_read_comes_to_the_owner():
    def down(path):
        raise RuntimeError("HTTP 500")

    delegation = run.read_delegation(down, REPO)
    assert isinstance(delegation, mandate.DelegationError)
    assert str(delegation) == "the settings can't be read"


def test_a_failed_intent_read_comes_to_the_owner_without_details():
    fake = Fake()

    def down(path):
        raise RuntimeError("HTTP 500 mentioning a private path")

    decision = run.evaluate(
        REPO, SHA, fake.get, down, lambda number: (2,), lambda intent: mandate.Acceptance(1, 1), lambda number: "", no_judge
    )
    assert decision.outcome == "ESCALATE"
    assert [reason.code for reason in decision.reasons] == ["INTENT_INVALID"]
    assert "private" not in mandate.message(decision)


def test_a_check_with_no_known_run_has_no_source():
    fake = Fake()
    real_get = fake.get

    def get(path):
        if "actions/runs?" in path:
            return {"total_count": 0, "workflow_runs": []}
        return real_get(path)

    [result, _] = run.check_results(get, REPO, SHA)
    assert (result.name, result.workflow, result.event) == ("test", "", "")


def test_a_check_is_traced_to_its_own_run():
    fake = Fake()
    real_get = fake.get

    def get(path):
        if "actions/runs?" in path:
            return {
                "total_count": 2,
                "workflow_runs": [
                    {"id": 10, "path": ".github/workflows/other.yml", "event": "push"},
                    {"id": 11, "path": ".github/workflows/ci.yml", "event": "pull_request"},
                ],
            }
        return real_get(path)

    [result, _] = run.check_results(get, REPO, SHA)
    assert (result.workflow, result.event) == (".github/workflows/ci.yml", "pull_request")


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


# --- asking the Judge --------------------------------------------------------

REAL_JUDGE = DELEGATION.replace('judge_model = "none"', 'judge_model = "a-model"')
DIFF = "+a change\n"


def judged(
    delegation=REAL_JUDGE,
    reply='{"score": 0.9, "reason": "Fine."}',
    linked=(2,),
    check="success",
    diff=DIFF,
    earlier=None,
    commits=(SHA,),
):
    fake = Fake(delegation=delegation, check_conclusion=check)
    fake.decisions = earlier or {}
    fake.pr_commits = [{"sha": sha} for sha in commits]
    asked = []

    def ask(model, prompt):
        asked.append((model, prompt))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_diff(number):
        if isinstance(diff, Exception):
            raise diff
        return diff

    decision = run.evaluate(
        REPO, SHA, fake.get, fake.get_private, lambda number: linked, lambda intent: mandate.Acceptance(1, 1), read_diff, ask
    )
    return decision, asked


def test_a_good_score_authorizes_and_shows_the_response():
    decision, asked = judged()
    assert decision.outcome == "AUTHORIZE"
    assert decision.judge_score == 0.9
    assert decision.judge_response == '{"score": 0.9, "reason": "Fine."}'
    [(model, prompt)] = asked
    assert model == "a-model"
    assert prompt.endswith("Change:\n+a change\n")
    assert "test_one" not in prompt
    assert "A private requirement." in prompt


def test_a_low_score_blocks():
    decision, _ = judged(reply='{"score": 0.4, "reason": "Extra job."}')
    assert decision.outcome == "BLOCK"
    assert [reason.code for reason in decision.reasons] == ["JUDGE_SCORE_LOW"]


def test_the_model_is_not_asked_when_there_is_no_judge():
    decision, asked = judged(delegation=DELEGATION)
    assert asked == []
    assert decision.outcome == "VERIFY"


@pytest.mark.parametrize("kwargs", [{"linked": ()}, {"check": "failure"}])
def test_the_model_is_not_asked_when_anything_else_failed(kwargs):
    decision, asked = judged(**kwargs)
    assert asked == []
    assert decision.outcome == "BLOCK"
    assert decision.judge_score is None


@pytest.mark.parametrize(
    "kwargs, shown",
    [
        ({"reply": RuntimeError("private detail")}, "The model gave no answer."),
        ({"diff": RuntimeError("private detail")}, "The change couldn't be read."),
        ({"diff": "x" * (mandate.MAX_DIFF_CHARS + 1)}, "The change is too large to judge."),
        ({"reply": "not json"}, "not json"),
    ],
)
def test_no_usable_answer_means_no_score(kwargs, shown):
    decision, _ = judged(**kwargs)
    assert decision.outcome == "VERIFY"
    assert [reason.code for reason in decision.reasons] == ["JUDGE_SCORE_MISSING"]
    assert decision.judge_score is None
    assert decision.judge_response == shown


def test_an_oversized_change_is_never_sent():
    _, asked = judged(diff="x" * (mandate.MAX_DIFF_CHARS + 1))
    assert asked == []


# --- one score per commit, and the refusal limit ------------------------------

OTHER = "e" * 40


def receipt(score=0.4, commit=SHA, model="a-model", intent_commit=INTENT_COMMIT, pr=7, code="JUDGE_SCORE_LOW", response="earlier"):
    record = {
        "pr": pr,
        "commit": commit,
        "judge_model": model,
        "intent_commit": intent_commit,
        "judge_score": score,
        "judge_response": response,
        "reasons": [{"code": code, "detail": "x"}] if code else [],
    }
    text = f"<details>\n\n```json\n{json.dumps(record)}\n```\n\n</details>\n"
    return {"app": {"slug": "github-actions"}, "output": {"text": text}}


def test_a_commit_keeps_its_first_score():
    decision, asked = judged(earlier={SHA: [receipt(score=0.4)]}, reply='{"score": 0.99, "reason": "Rerolled."}')
    assert asked == []
    assert decision.outcome == "BLOCK"
    assert decision.judge_score == 0.4
    assert decision.judge_response == "earlier"


def test_the_newest_earlier_score_is_used():
    newest, oldest = receipt(score=0.9, code=None, response="newest"), receipt(score=0.4, response="oldest")
    decision, asked = judged(earlier={SHA: [newest, oldest]})
    assert asked == []
    assert decision.judge_score == 0.9


@pytest.mark.parametrize(
    "earlier",
    [
        receipt(model="another-model"),
        receipt(intent_commit="f" * 40),
        receipt(score=None),
        receipt(pr=8),
        {"app": {"slug": "another-app"}, "output": {"text": receipt()["output"]["text"]}},
        {"app": {"slug": "github-actions"}, "output": {"text": "not a receipt"}},
        {"app": {"slug": "github-actions"}, "output": {}},
    ],
)
def test_a_score_only_carries_over_when_it_applies(earlier):
    decision, asked = judged(earlier={SHA: [earlier]})
    assert len(asked) == 1
    assert decision.judge_score == 0.9


def test_a_new_commit_gets_a_new_score():
    decision, asked = judged(earlier={OTHER: [receipt(commit=OTHER, score=0.4)]}, commits=(OTHER, SHA))
    assert len(asked) == 1
    assert decision.judge_score == 0.9


def refused(*shas):
    return {sha: [receipt(commit=sha)] for sha in shas}


def test_the_third_refusal_goes_to_the_owner():
    first, second = "1" * 40, "2" * 40
    decision, asked = judged(
        earlier=refused(first, second), commits=(first, second, SHA), reply='{"score": 0.4, "reason": "Still bad."}'
    )
    assert len(asked) == 1
    assert decision.outcome == "ESCALATE"
    assert [reason.code for reason in decision.reasons] == ["JUDGE_REFUSALS"]


def test_after_three_refusals_the_model_is_not_asked_again():
    first, second, third = "1" * 40, "2" * 40, "3" * 40
    decision, asked = judged(earlier=refused(first, second, third), commits=(first, second, third, SHA))
    assert asked == []
    assert decision.outcome == "ESCALATE"
    assert decision.judge_score is None


def test_reruns_on_one_commit_count_as_one_refusal():
    first = "1" * 40
    earlier = {first: [receipt(commit=first), receipt(commit=first), receipt(commit=first)]}
    decision, asked = judged(earlier=earlier, commits=(first, SHA), reply='{"score": 0.4, "reason": "Bad."}')
    assert len(asked) == 1
    assert decision.outcome == "BLOCK"


def test_earlier_passes_do_not_count_as_refusals():
    first, second, third = "1" * 40, "2" * 40, "3" * 40
    passed = {sha: [receipt(commit=sha, score=0.9, code=None)] for sha in (first, second, third)}
    decision, asked = judged(earlier=passed, commits=(first, second, third, SHA))
    assert len(asked) == 1
    assert decision.outcome == "AUTHORIZE"


def test_an_escalated_refusal_counts_too():
    first, second, third = "1" * 40, "2" * 40, "3" * 40
    earlier = refused(first, second) | {third: [receipt(commit=third, code="JUDGE_REFUSALS")]}
    _, asked = judged(earlier=earlier, commits=(first, second, third, SHA))
    assert asked == []


def test_a_rerun_of_a_third_refusal_keeps_its_score_and_its_count():
    first, second = "1" * 40, "2" * 40
    earlier = refused(first, second) | {SHA: [receipt(commit=SHA, score=0.4, code="JUDGE_REFUSALS")]}
    decision, asked = judged(earlier=earlier, commits=(first, second, SHA))
    assert asked == []
    assert decision.outcome == "ESCALATE"
    assert decision.judge_score == 0.4
    assert decision.reasons[0].detail == "the Judge has refused this pull request 3 times"
