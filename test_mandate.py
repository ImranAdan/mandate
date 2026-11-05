import base64
import json

import pytest

import mandate

HEAD = "a" * 40
DELEGATION_COMMIT = "b" * 40
CONFIG = """
repository = "ImranAdan/tasks-api"
dry_run = true
judge_model = "none"
judge_threshold = 0.8
intent_repo = "ImranAdan/mandate-intent"

[proofs.tests]
check = "test"
workflow = ".github/workflows/ci.yml"
"""
SCORING = CONFIG.replace('judge_model = "none"', 'judge_model = "some-model"')
LIVE = SCORING.replace("dry_run = true", "dry_run = false")
INTENT_COMMIT = "d" * 40
BOUND = mandate.Intent("A title", 2, (), INTENT_COMMIT)


def load(text=CONFIG):
    return mandate.load_delegation(text, "ImranAdan/tasks-api", DELEGATION_COMMIT)


def check(**changes):
    fields = {
        "name": "test",
        "workflow": ".github/workflows/ci.yml",
        "event": "pull_request",
        "app": "github-actions",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
    }
    return mandate.CheckResult(**(fields | changes))


def decide(delegation=None, paths=("tasks_api.py",), checks=None, score=None, intent=BOUND):
    return mandate.decide(
        load(SCORING) if delegation is None else delegation,
        pr=12,
        head_sha=HEAD,
        changed_paths=paths,
        checks=(check(),) if checks is None else checks,
        intent=intent,
        judge_score=score,
    )


def codes(decision):
    return [reason.code for reason in decision.reasons]


# --- settings ---------------------------------------------------------------


def test_settings_load_as_written():
    delegation = load(LIVE)
    assert delegation.repository == "ImranAdan/tasks-api"
    assert delegation.commit == DELEGATION_COMMIT
    assert delegation.dry_run is False
    assert delegation.judge_model == "some-model"
    assert delegation.judge_threshold == 0.8
    assert delegation.judge_reads_pr_text is False
    assert delegation.intent_repo == "ImranAdan/mandate-intent"
    assert delegation.proofs == (mandate.Proof("tests", "test", ".github/workflows/ci.yml"),)
    assert delegation.warnings == ()


def test_a_missing_dry_run_counts_as_true_with_a_warning():
    delegation = load(LIVE.replace("dry_run = false\n", ""))
    assert delegation.dry_run is True
    assert delegation.warnings == ("dry_run is missing; forcing dry_run to true",)


def test_no_judge_forces_dry_run_with_a_warning():
    delegation = load(CONFIG.replace("dry_run = true", "dry_run = false"))
    assert delegation.dry_run is True
    assert delegation.warnings == ('judge_model is "none"; forcing dry_run to true',)


def test_no_judge_with_dry_run_already_on_gives_no_warning():
    assert load(CONFIG).warnings == ()


def test_a_missing_judge_model_counts_as_none():
    delegation = load(LIVE.replace('judge_model = "some-model"\n', ""))
    assert delegation.judge_model == "none"
    assert delegation.dry_run is True
    assert delegation.warnings == (
        "judge_model is missing; using none",
        'judge_model is "none"; forcing dry_run to true',
    )


def test_optional_settings_have_safe_defaults():
    delegation = load(SCORING.replace("judge_threshold = 0.8\n", ""))
    assert delegation.judge_threshold == 0.8
    assert delegation.judge_reads_pr_text is False


@pytest.mark.parametrize(
    "text, message",
    [
        ("not toml =", "not valid TOML"),
        ('surprise = "x"\n' + CONFIG, "unknown settings: surprise"),
        (CONFIG.replace("ImranAdan/tasks-api", "ImranAdan/other"), "written for ImranAdan/other"),
        (CONFIG.replace('repository = "ImranAdan/tasks-api"\n', ""), "repository is required"),
        (CONFIG.replace("dry_run = true", 'dry_run = "false"'), "dry_run must be true or false"),
        (CONFIG.replace('judge_model = "none"', "judge_model = 1"), "judge_model must be a name"),
        (CONFIG.replace('judge_model = "none"', 'judge_model = ""'), "judge_model must be a name"),
        (CONFIG.replace("judge_threshold = 0.8", "judge_threshold = 1.5"), "judge_threshold must be from 0 to 1"),
        (CONFIG.replace("judge_threshold = 0.8", "judge_threshold = -0.1"), "judge_threshold must be from 0 to 1"),
        (CONFIG.replace("judge_threshold = 0.8", "judge_threshold = true"), "judge_threshold must be from 0 to 1"),
        (CONFIG.replace("judge_threshold = 0.8", 'judge_threshold = "0.8"'), "judge_threshold must be from 0 to 1"),
        ('judge_reads_pr_text = "no"\n' + CONFIG, "judge_reads_pr_text must be true or false"),
        (CONFIG.replace('intent_repo = "ImranAdan/mandate-intent"\n', ""), "intent_repo must be owner/name"),
        (CONFIG.replace("ImranAdan/mandate-intent", "mandate-intent"), "intent_repo must be owner/name"),
        (CONFIG.split("[proofs.tests]")[0], "at least one proof is required"),
        (CONFIG.split("[proofs.tests]")[0] + "[proofs]\n", "at least one proof is required"),
        (CONFIG.replace('check = "test"\n', ""), "proof tests needs check and workflow"),
        (CONFIG.replace('workflow = ".github/workflows/ci.yml"', 'workflow = "ci.yml"'), "proof tests must name a workflow"),
        (CONFIG + 'extra = "x"\n', "proof tests has unknown settings: extra"),
    ],
)
def test_a_bad_delegation_is_refused(text, message):
    with pytest.raises(mandate.DelegationError, match=message):
        load(text)


# --- the decision ------------------------------------------------------------


def test_no_judge_yet_waits_for_the_score():
    decision = decide(load(CONFIG))
    assert decision.outcome == "VERIFY"
    assert codes(decision) == ["JUDGE_SCORE_MISSING"]
    assert decision.judge_score is None


def test_every_proof_and_a_good_score_authorize():
    decision = decide(score=0.92)
    assert decision.outcome == "AUTHORIZE"
    assert decision.reasons == ()
    assert decision.judge_score == 0.92


def test_a_score_at_the_threshold_passes():
    assert decide(score=0.8).outcome == "AUTHORIZE"


def test_a_score_below_the_threshold_blocks():
    decision = decide(score=0.79)
    assert decision.outcome == "BLOCK"
    assert codes(decision) == ["JUDGE_SCORE_LOW"]
    assert decision.reasons[0].detail == "JudgeScore 0.79 is below 0.8"


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf"), True, "0.9"])
def test_an_invalid_score_counts_as_missing(score):
    decision = decide(score=score)
    assert decision.outcome == "VERIFY"
    assert codes(decision) == ["JUDGE_SCORE_MISSING"]
    assert decision.judge_score is None


def test_a_score_is_ignored_when_there_is_no_judge():
    decision = decide(load(CONFIG), score=0.99)
    assert decision.outcome == "VERIFY"
    assert decision.judge_score is None


@pytest.mark.parametrize(
    "checks, outcome, expected",
    [
        ((), "VERIFY", ["PROOF_MISSING"]),
        ((check(name="lint"),), "VERIFY", ["PROOF_MISSING"]),
        ((check(head_sha="c" * 40),), "VERIFY", ["PROOF_MISSING"]),
        ((check(status="in_progress", conclusion=None),), "VERIFY", ["PROOF_PENDING"]),
        ((check(conclusion="failure"),), "BLOCK", ["PROOF_FAILED"]),
        ((check(conclusion="skipped"),), "BLOCK", ["PROOF_FAILED"]),
        ((check(conclusion="cancelled"),), "BLOCK", ["PROOF_FAILED"]),
        ((check(), check(conclusion="failure")), "BLOCK", ["PROOF_CONFLICT"]),
        ((check(), check(status="in_progress", conclusion=None)), "VERIFY", ["PROOF_PENDING"]),
    ],
)
def test_a_high_score_cannot_rescue_a_proof(checks, outcome, expected):
    decision = decide(checks=checks, score=0.99)
    assert decision.outcome == outcome
    assert codes(decision) == expected
    assert decision.judge_score is None


@pytest.mark.parametrize(
    "forged",
    [
        check(workflow=".github/workflows/other.yml"),
        check(event="push"),
        check(app="some-other-app"),
    ],
)
def test_a_check_from_the_wrong_source_blocks(forged):
    alone = decide(checks=(forged,), score=0.99)
    assert alone.outcome == "BLOCK"
    assert codes(alone) == ["CHECK_WRONG_SOURCE", "PROOF_MISSING"]

    beside_a_genuine_one = decide(checks=(check(), forged), score=0.99)
    assert beside_a_genuine_one.outcome == "BLOCK"
    assert codes(beside_a_genuine_one) == ["CHECK_WRONG_SOURCE"]


@pytest.mark.parametrize(
    "path, code",
    [
        (".github/workflows/ci.yml", "RESERVED:automation"),
        (".github/dependabot.yml", "RESERVED:automation"),
        (".github/actions/x/action.yml", "RESERVED:automation"),
        (".GitHub/workflows/ci.yml", "RESERVED:automation"),
        ("CODEOWNERS", "RESERVED:automation"),
        ("docs/CODEOWNERS", "RESERVED:automation"),
        ("AGENTS.md", "RESERVED:agent-instructions"),
        ("sub/CLAUDE.md", "RESERVED:agent-instructions"),
        (".claude/settings.json", "RESERVED:agent-instructions"),
        ("schema.sql", "RESERVED:migration"),
        ("db/schema.prisma", "RESERVED:migration"),
        ("migrations/0002.sql", "RESERVED:migration"),
        ("app/migration/0002.py", "RESERVED:migration"),
        ("alembic/versions/1.py", "RESERVED:migration"),
    ],
)
def test_a_reserved_path_comes_to_the_owner_whatever_the_score(path, code):
    decision = decide(paths=("tasks_api.py", path), score=0.99)
    assert decision.outcome == "ESCALATE"
    assert codes(decision) == [code]
    assert decision.judge_score is None


@pytest.mark.parametrize(
    "path",
    [
        "tasks_api.py",
        "test_tasks_api.py",
        "README.md",
        "docs/github.md",
        "not.github/x.yml",
        "not_schema.sql",
        "migration_notes.md",
        "NOT_AGENTS.md",
        "pyproject.toml",
    ],
)
def test_an_ordinary_path_is_not_reserved(path):
    assert decide(paths=(path,), score=0.9).outcome == "AUTHORIZE"


def test_a_moved_workflow_stays_reserved_through_its_old_path():
    decision = decide(paths=(".github/workflows/ci.yml", "ci.yml"), score=0.99)
    assert decision.outcome == "ESCALATE"


def test_paths_in_one_category_share_one_reason():
    decision = decide(paths=("schema.sql", "migrations/1.sql"), score=0.99)
    assert codes(decision) == ["RESERVED:migration"]
    assert decision.reasons[0].detail == "migrations/1.sql, schema.sql"


def test_a_failed_proof_outranks_a_reserved_path():
    decision = decide(paths=("schema.sql",), checks=(check(conclusion="failure"),))
    assert decision.outcome == "BLOCK"
    assert codes(decision) == ["PROOF_FAILED", "RESERVED:migration"]


def test_a_reserved_path_outranks_a_missing_proof():
    decision = decide(paths=("schema.sql",), checks=())
    assert decision.outcome == "ESCALATE"
    assert codes(decision) == ["RESERVED:migration", "PROOF_MISSING"]


def test_a_pr_with_no_changes_comes_to_the_owner():
    decision = decide(paths=(), score=0.99)
    assert decision.outcome == "ESCALATE"
    assert codes(decision) == ["NO_CHANGES"]


def test_an_invalid_delegation_comes_to_the_owner_and_merges_nothing():
    decision = decide(mandate.DelegationError("unknown settings: surprise"), score=0.99)
    assert decision.outcome == "ESCALATE"
    assert codes(decision) == ["DELEGATION_INVALID"]
    assert decision.reasons[0].detail == "unknown settings: surprise"
    assert decision.dry_run is True
    assert decision.judge_score is None


# --- the receipt -------------------------------------------------------------


@pytest.mark.parametrize(
    "decision, text",
    [
        (
            decide(load(LIVE), score=0.92),
            "PR #12, commit aaaaaaa, JudgeScore 0.92, AUTHORIZE\nReady to merge.",
        ),
        (
            decide(load(SCORING), score=0.92),
            "PR #12, commit aaaaaaa, JudgeScore 0.92, AUTHORIZE\n"
            "Ready to merge, but this is a dry run, so it wasn't merged.",
        ),
        (
            decide(score=0.4),
            "PR #12, commit aaaaaaa, JudgeScore 0.4, BLOCK\nNot ready: JudgeScore 0.4 is below 0.8.",
        ),
        (
            decide(load(CONFIG)),
            "PR #12, commit aaaaaaa, JudgeScore missing, VERIFY\nWaiting for proof: JudgeScore missing.",
        ),
        (
            decide(paths=("schema.sql",), score=0.99),
            "PR #12, commit aaaaaaa, JudgeScore missing, ESCALATE\nNeeds the owner: schema.sql.",
        ),
        (
            decide(checks=(check(conclusion="failure"),)),
            "PR #12, commit aaaaaaa, JudgeScore missing, BLOCK\nNot ready: proof tests failed.",
        ),
        (
            decide(load(CONFIG.replace("dry_run = true", "dry_run = false"))),
            "PR #12, commit aaaaaaa, JudgeScore missing, VERIFY\n"
            "Waiting for proof: JudgeScore missing.\n"
            'WARNING: judge_model is "none"; forcing dry_run to true',
        ),
    ],
)
def test_the_receipt_reads_plainly(decision, text):
    assert mandate.receipt_text(decision) == text


def test_only_the_worst_outcomes_reasons_reach_the_message():
    decision = decide(paths=("schema.sql",), checks=(check(conclusion="failure"),))
    assert mandate.message(decision) == "Not ready: proof tests failed."


def test_the_record_keeps_exact_identifiers():
    record = mandate.receipt_record(decide(load(LIVE), score=0.92))
    assert record == {
        "repository": "ImranAdan/tasks-api",
        "pr": 12,
        "commit": HEAD,
        "delegation_commit": DELEGATION_COMMIT,
        "intent_issue": 2,
        "intent_commit": INTENT_COMMIT,
        "judge_model": "some-model",
        "judge_score": 0.92,
        "dry_run": False,
        "outcome": "AUTHORIZE",
        "reasons": [],
        "message": "Ready to merge.",
        "warnings": [],
    }
    assert json.loads(json.dumps(record)) == record


def test_the_record_of_an_invalid_delegation_has_no_settings_to_name():
    record = mandate.receipt_record(decide(mandate.DelegationError("bad")))
    assert record["repository"] is None
    assert record["delegation_commit"] is None
    assert record["intent_issue"] is None
    assert record["intent_commit"] is None
    assert record["judge_model"] is None
    assert record["dry_run"] is True
    assert record["message"] == "Needs the owner: bad."


# --- the intent --------------------------------------------------------------

INTENT = """# Filter tasks by done state

Issue: #2

## Feature

`GET /tasks` accepts an optional `done` filter.

- `done=false` returns only open tasks. Test: `test_open_only`
- An invalid value returns 422. Test: `test_bad_filter`

## Security

- The code contains no secrets. Judge only

## Out of scope

- Search, sorting and paging.
"""
TESTS = """
def helper():
    pass

def test_open_only():
    pass

def test_bad_filter():
    pass
"""


def test_an_intent_lists_its_criteria_and_their_proof():
    intent = mandate.load_intent(INTENT, TESTS)
    assert intent.title == "Filter tasks by done state"
    assert intent.issue == 2
    assert intent.criteria == (
        mandate.Criterion("Feature", "`done=false` returns only open tasks.", "test_open_only"),
        mandate.Criterion("Feature", "An invalid value returns 422.", "test_bad_filter"),
        mandate.Criterion("Security", "The code contains no secrets.", None),
    )
    assert intent.commit == ""
    assert mandate.load_intent(INTENT, TESTS, INTENT_COMMIT).commit == INTENT_COMMIT


def test_the_first_heading_is_the_title():
    assert mandate.load_intent(INTENT + "\n# Another heading\n", TESTS).title == "Filter tasks by done state"


def test_any_section_name_works():
    text = INTENT.replace("## Security", "## Performance")
    assert mandate.load_intent(text, TESTS).criteria[2].section == "Performance"


@pytest.mark.parametrize(
    "text, tests, message",
    [
        (INTENT.replace("# Filter tasks by done state\n", ""), TESTS, "needs a title"),
        (INTENT.replace("Issue: #2\n", ""), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("Issue: #2", "Issue: #2\nIssue: #3"), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("Issue: #2", "Issue: 2"), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("Issue: #2", "Issue: #0"), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("Issue: #2", "Issue: #2\nIssue: #0"), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("Issue: #2", "Issue: #2\nissue: #3"), TESTS, "needs exactly one Issue line"),
        (INTENT.replace("## Security\n", "## Security\n\n- \n"), TESTS, "needs a test or Judge only: -"),
        (
            INTENT.replace("## Security\n", "## Security\n\n```\n## Out of scope\n```\n\n- Hidden by the example.\n"),
            TESTS,
            "needs a test or Judge only: - Hidden by the example.",
        ),
        (INTENT.replace("## Security\n", "## Security\n\n```\n"), TESTS, "has a code block that never closes"),
        (INTENT.replace(" Judge only", ""), TESTS, "needs a test or Judge only: - The code contains no secrets."),
        (INTENT.replace("- The code", "* The code").replace(" Judge only", ""), TESTS, "needs a test or Judge only"),
        (INTENT.replace("- The code", "1. The code").replace(" Judge only", ""), TESTS, "needs a test or Judge only"),
        (INTENT.replace("Test: `test_open_only`", "Test: test_open_only"), TESTS, "needs a test or Judge only"),
        (INTENT.replace("Test: `test_open_only`", "Test: `open_only`"), TESTS, "needs a test or Judge only"),
        (INTENT.replace("test_open_only", "test_missing"), TESTS, "names tests that don't exist: test_missing"),
        (INTENT, "def test_bad_filter():\n    pass\n", "names tests that don't exist: test_open_only"),
        (INTENT, "class Holder:\n    def test_open_only(self):\n        pass\ndef test_bad_filter():\n    pass\n", "names tests that don't exist: test_open_only"),
        (INTENT, "def test_open_only(:", "its tests can't be read"),
        ("# Title\n\nIssue: #2\n\n## Security\n\n- No secrets. Judge only\n", TESTS, "needs at least one named test"),
        ("# Title\n\nIssue: #2\n", TESTS, "needs at least one named test"),
    ],
)
def test_a_bad_intent_is_refused(text, tests, message):
    with pytest.raises(mandate.IntentError, match=message.replace("*", r"\*")):
        mandate.load_intent(text, tests)


@pytest.mark.parametrize("fence", ["```", "~~~", "  ```python"])
def test_a_code_block_is_free_text(fence):
    closing = fence.strip()[:3]
    text = INTENT.replace("## Security\n", f"## Security\n\n{fence}\n- An example, not a criterion.\nIssue: #9\n{closing}\n")
    intent = mandate.load_intent(text, TESTS)
    assert intent.issue == 2
    assert len(intent.criteria) == 3


def test_out_of_scope_needs_no_proof_in_any_case():
    text = INTENT.replace("## Out of scope", "## OUT OF SCOPE")
    assert len(mandate.load_intent(text, TESTS).criteria) == 3


def test_a_criterion_after_out_of_scope_still_needs_proof():
    text = INTENT + "\n## Reliability\n\n- It never crashes.\n"
    with pytest.raises(mandate.IntentError, match="needs a test or Judge only: - It never crashes."):
        mandate.load_intent(text, TESTS)


# --- finding a pull request's intent -----------------------------------------


def test_a_pr_linking_one_claimed_issue_finds_its_intent():
    other = mandate.Intent("Other", 3, ())
    assert mandate.find_intent((2,), (other, BOUND)) is BOUND


@pytest.mark.parametrize(
    "linked, message",
    [
        ((), "link exactly one issue"),
        ((2, 3), "link exactly one issue"),
        ((9,), "no intent claims issue #9"),
    ],
)
def test_a_pr_without_one_claimed_issue_is_unbound(linked, message):
    with pytest.raises(mandate.IntentUnbound, match=message):
        mandate.find_intent(linked, (BOUND, mandate.Intent("Other", 3, ())))


def test_two_intents_claiming_one_issue_are_invalid():
    with pytest.raises(mandate.IntentError, match="more than one intent claims issue #2"):
        mandate.find_intent((2,), (BOUND, mandate.Intent("Other", 2, ())))


def test_an_unbound_pr_blocks_whatever_the_score():
    decision = decide(score=0.99, intent=mandate.IntentUnbound("no intent claims issue #9"))
    assert decision.outcome == "BLOCK"
    assert codes(decision) == ["INTENT_UNBOUND"]
    assert decision.judge_score is None
    assert mandate.message(decision) == "Not ready: no intent claims issue #9."
    assert mandate.receipt_record(decision)["intent_issue"] is None


def test_an_invalid_intent_comes_to_the_owner_without_quoting_it():
    private = "needs a test or Judge only: - A private requirement."
    decision = decide(score=0.99, intent=mandate.IntentError(private))
    assert decision.outcome == "ESCALATE"
    assert codes(decision) == ["INTENT_INVALID"]
    assert decision.judge_score is None
    assert mandate.message(decision) == "Needs the owner: the intent can't be used."
    assert "private" not in json.dumps(mandate.receipt_record(decision))


# --- reading intents ---------------------------------------------------------


def content(text):
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def github(files):
    """A fake GitHub holding files in one folder at one commit."""
    calls = []

    def get(path):
        calls.append(path)
        if path == "repos/o/intents/commits/main":
            return {"sha": INTENT_COMMIT}
        if path == f"repos/o/intents/contents/tasks-api?ref={INTENT_COMMIT}":
            return [{"name": name, "type": "file"} for name in files]
        for name, text in files.items():
            if path == f"repos/o/intents/contents/tasks-api/{name}?ref={INTENT_COMMIT}":
                return content(text)
        raise RuntimeError(f"not found: {path}")

    get.calls = calls
    return get


def test_intents_are_all_read_at_one_commit():
    get = github({"filter-by-done.md": INTENT, "test_filter_by_done.py": TESTS})
    intents = mandate.read_intents(get, "o/intents", "tasks-api")
    assert [(i.title, i.issue, i.commit) for i in intents] == [("Filter tasks by done state", 2, INTENT_COMMIT)]
    assert get.calls[0] == "repos/o/intents/commits/main"
    assert all(f"ref={INTENT_COMMIT}" in call for call in get.calls[1:])


@pytest.mark.parametrize(
    "files, message",
    [
        ({"filter-by-done.md": INTENT}, "filter-by-done.md has no test_filter_by_done.py"),
        ({"bad.md": "no title", "test_bad.py": TESTS}, "bad.md: needs a title"),
    ],
)
def test_one_bad_intent_refuses_them_all(files, message):
    good = {"filter-by-done.md": INTENT, "test_filter_by_done.py": TESTS} if "bad.md" in files else {}
    with pytest.raises(mandate.IntentError, match=message):
        mandate.read_intents(github(good | files), "o/intents", "tasks-api")


def test_a_read_failure_is_never_read_as_no_intents():
    def down(path):
        raise RuntimeError("HTTP 500 with a private detail")

    with pytest.raises(mandate.IntentError, match="intents can't be read") as caught:
        mandate.read_intents(down, "o/intents", "tasks-api")
    assert "private" not in str(caught.value)
