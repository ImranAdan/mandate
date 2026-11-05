"""Decide whether an agent's pull request may merge, and record why.

decide() is a pure function of what GitHub reported and the owner's settings.
The outcome is the worst reason's: BLOCK > ESCALATE > VERIFY > AUTHORIZE, and
AUTHORIZE means there is no reason at all. PR text is never an input, and a
JudgeScore can refuse a merge but can't authorize one.
"""

import re
import tomllib
from dataclasses import dataclass

GITHUB_ACTIONS = "github-actions"
PULL_REQUEST = "pull_request"
NO_JUDGE = "none"
DEFAULT_THRESHOLD = 0.8
OUTCOMES = ("BLOCK", "ESCALATE", "VERIFY", "AUTHORIZE")  # worst first
SETTINGS = frozenset(
    {"repository", "dry_run", "judge_model", "judge_threshold", "judge_reads_pr_text", "intent_repo", "proofs"}
)
REPOSITORY = re.compile(r"[^/\s]+/[^/\s]+")

# ponytail: reserved by path alone, so any .github/ change comes to the owner and
# SQL written inside application code isn't seen. The Judge has to catch that;
# parse what changed if these interruptions or that gap become a real cost.
RESERVED = (
    ("automation", re.compile(r"^\.github/|(^|/)CODEOWNERS$", re.I)),
    ("agent-instructions", re.compile(r"^\.claude/|(^|/)(AGENTS|CLAUDE)\.md$", re.I)),
    ("migration", re.compile(r"(^|/)(migrations?|alembic)/|(^|/)schema\.(sql|prisma)$", re.I)),
)
MESSAGES = {"BLOCK": "Not ready", "ESCALATE": "Needs the owner", "VERIFY": "Waiting for proof"}


class DelegationError(ValueError):
    """The owner's settings can't be used, so every pull request comes to the owner."""


@dataclass(frozen=True)
class Proof:
    name: str
    check: str
    workflow: str


@dataclass(frozen=True)
class Delegation:
    repository: str
    commit: str
    dry_run: bool
    judge_model: str
    judge_threshold: float
    judge_reads_pr_text: bool
    intent_repo: str
    proofs: tuple[Proof, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    """One check run as GitHub recorded it."""

    name: str
    workflow: str
    event: str
    app: str
    head_sha: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Reason:
    code: str
    outcome: str
    detail: str


@dataclass(frozen=True)
class Decision:
    delegation: Delegation | None
    pr: int
    head_sha: str
    outcome: str
    reasons: tuple[Reason, ...]
    judge_score: float | None
    dry_run: bool


def load_delegation(text: str, repository: str, commit: str) -> Delegation:
    """The owner's settings for one repository, read strictly from one commit."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise DelegationError(f"not valid TOML: {error}") from error

    unknown = sorted(set(data) - SETTINGS)
    if unknown:
        raise DelegationError(f"unknown settings: {', '.join(unknown)}")
    if "repository" not in data:
        raise DelegationError("repository is required")
    if data["repository"] != repository:
        raise DelegationError(f"written for {data['repository']}, not {repository}")

    warnings = []
    dry_run = data.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise DelegationError("dry_run must be true or false")
    if "dry_run" not in data:
        warnings.append("dry_run is missing; forcing dry_run to true")

    judge_model = data.get("judge_model", NO_JUDGE)
    if not isinstance(judge_model, str) or not judge_model:
        raise DelegationError("judge_model must be a name")
    if "judge_model" not in data:
        warnings.append(f"judge_model is missing; using {NO_JUDGE}")
    if judge_model == NO_JUDGE and dry_run is False:
        warnings.append(f'judge_model is "{NO_JUDGE}"; forcing dry_run to true')
    if judge_model == NO_JUDGE:
        dry_run = True

    threshold = data.get("judge_threshold", DEFAULT_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise DelegationError("judge_threshold must be from 0 to 1")

    reads_pr_text = data.get("judge_reads_pr_text", False)
    if not isinstance(reads_pr_text, bool):
        raise DelegationError("judge_reads_pr_text must be true or false")

    intent_repo = data.get("intent_repo")
    if not isinstance(intent_repo, str) or not REPOSITORY.fullmatch(intent_repo):
        raise DelegationError("intent_repo must be owner/name")

    return Delegation(
        repository=repository,
        commit=commit,
        dry_run=dry_run,
        judge_model=judge_model,
        judge_threshold=threshold,
        judge_reads_pr_text=reads_pr_text,
        intent_repo=intent_repo,
        proofs=_proofs(data.get("proofs")),
        warnings=tuple(warnings),
    )


def _proofs(table: object) -> tuple[Proof, ...]:
    if not isinstance(table, dict) or not table:
        raise DelegationError("at least one proof is required")
    proofs = []
    for name, proof in table.items():
        if not isinstance(proof, dict) or not isinstance(proof.get("check"), str) or "workflow" not in proof:
            raise DelegationError(f"proof {name} needs check and workflow")
        unknown = sorted(set(proof) - {"check", "workflow"})
        if unknown:
            raise DelegationError(f"proof {name} has unknown settings: {', '.join(unknown)}")
        workflow = proof["workflow"]
        if not isinstance(workflow, str) or not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", workflow):
            raise DelegationError(f"proof {name} must name a workflow in .github/workflows")
        proofs.append(Proof(name, proof["check"], workflow))
    return tuple(proofs)


def decide(
    delegation: Delegation | DelegationError,
    pr: int,
    head_sha: str,
    changed_paths: tuple[str, ...],
    checks: tuple[CheckResult, ...],
    judge_score: object = None,
) -> Decision:
    """One outcome and every reason for one exact commit of a pull request.

    A rename's old path belongs in changed_paths, so moving a reserved file is
    still a change to it.
    """
    if isinstance(delegation, DelegationError):
        reasons = (Reason("DELEGATION_INVALID", "ESCALATE", str(delegation)),)
        return Decision(None, pr, head_sha, "ESCALATE", reasons, None, True)

    proofs = _proof_reasons(delegation, head_sha, checks)
    reserved = _reserved_reasons(changed_paths)
    reasons = [*proofs, *reserved]
    score = None
    if not reasons:
        score, judged = _judged(delegation, judge_score)
        reasons += judged
    reasons.sort(key=lambda r: (OUTCOMES.index(r.outcome), r.code))
    outcome = reasons[0].outcome if reasons else "AUTHORIZE"
    return Decision(delegation, pr, head_sha, outcome, tuple(reasons), score, delegation.dry_run)


def _proof_reasons(delegation: Delegation, head_sha: str, checks: tuple[CheckResult, ...]) -> list[Reason]:
    reasons = []
    for proof in delegation.proofs:
        named = [check for check in checks if check.name == proof.check]
        genuine = [c for c in named if (c.workflow, c.event, c.app) == (proof.workflow, PULL_REQUEST, GITHUB_ACTIONS)]
        if len(genuine) != len(named):
            detail = f"check {proof.check} came from the wrong workflow, event or app"
            reasons.append(Reason("CHECK_WRONG_SOURCE", "BLOCK", detail))

        current = [check for check in genuine if check.head_sha == head_sha]
        passed = [check for check in current if check.status == "completed" and check.conclusion == "success"]
        finished = [check for check in current if check.status == "completed"]
        if not current:
            reasons.append(Reason("PROOF_MISSING", "VERIFY", f"proof {proof.name} is missing"))
        elif len(finished) != len(current):
            reasons.append(Reason("PROOF_PENDING", "VERIFY", f"proof {proof.name} is still running"))
        elif not passed:
            reasons.append(Reason("PROOF_FAILED", "BLOCK", f"proof {proof.name} failed"))
        elif len(passed) != len(finished):
            reasons.append(Reason("PROOF_CONFLICT", "BLOCK", f"proof {proof.name} has conflicting results"))
    return reasons


def _reserved_reasons(changed_paths: tuple[str, ...]) -> list[Reason]:
    if not changed_paths:
        return [Reason("NO_CHANGES", "ESCALATE", "the pull request changes nothing")]
    reasons = []
    for category, pattern in RESERVED:
        paths = sorted(path for path in changed_paths if pattern.search(path))
        if paths:
            reasons.append(Reason(f"RESERVED:{category}", "ESCALATE", ", ".join(paths)))
    return reasons


def _judged(delegation: Delegation, score: object) -> tuple[float | None, list[Reason]]:
    """A score counts only from a real Judge, and only as a number from 0 to 1."""
    valid = (
        delegation.judge_model != NO_JUDGE
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and 0 <= score <= 1  # also false for NaN
    )
    if not valid:
        return None, [Reason("JUDGE_SCORE_MISSING", "VERIFY", "JudgeScore missing")]
    if score < delegation.judge_threshold:
        detail = f"JudgeScore {score} is below {delegation.judge_threshold}"
        return score, [Reason("JUDGE_SCORE_LOW", "BLOCK", detail)]
    return score, []


def message(decision: Decision) -> str:
    """A short message from the outcome. Its wording never comes from a model."""
    if decision.outcome == "AUTHORIZE":
        if decision.dry_run:
            return "Ready to merge, but this is a dry run, so it wasn't merged."
        return "Ready to merge."
    details = "; ".join(r.detail for r in decision.reasons if r.outcome == decision.outcome)
    return f"{MESSAGES[decision.outcome]}: {details}."


def receipt_text(decision: Decision) -> str:
    score = "missing" if decision.judge_score is None else decision.judge_score
    lines = [
        f"PR #{decision.pr}, commit {decision.head_sha[:7]}, JudgeScore {score}, {decision.outcome}",
        message(decision),
    ]
    warnings = decision.delegation.warnings if decision.delegation else ()
    return "\n".join([*lines, *(f"WARNING: {warning}" for warning in warnings)])


def receipt_record(decision: Decision) -> dict[str, object]:
    # ponytail: a plain record resists tampering less than a hash-chained ledger;
    # chain the records if this is ever used anywhere more serious than a small app.
    delegation = decision.delegation
    return {
        "repository": delegation.repository if delegation else None,
        "pr": decision.pr,
        "commit": decision.head_sha,
        "delegation_commit": delegation.commit if delegation else None,
        "judge_model": delegation.judge_model if delegation else None,
        "judge_score": decision.judge_score,
        "dry_run": decision.dry_run,
        "outcome": decision.outcome,
        "reasons": [{"code": r.code, "detail": r.detail} for r in decision.reasons],
        "message": message(decision),
        "warnings": list(delegation.warnings) if delegation else [],
    }
