"""Validate frozen inputs, reviewer-only expectations and exact Git evidence."""

from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
from typing import Annotated, Literal

from git import Repo
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Category = Literal["understanding", "localization", "planning", "unanswerable"]
Dimension = Literal["correctness", "evidence", "completeness", "boundaries"]
Rating = Annotated[int, Field(ge=0, le=2)]
DIMENSIONS = ("correctness", "evidence", "completeness", "boundaries")
PAYLOADS = ("cases.jsonl", "expectations.jsonl", "evidence.jsonl", "rubric.json", "review-template.jsonl")


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Turn(Record):
    turn_id: Text
    mode: Literal["qa", "plan"]
    question: Text


class Case(Record):
    case_id: Text
    category: Category
    source_id: Text
    turns: Annotated[list[Turn], Field(min_length=1, max_length=3)]


class EvidenceGroup(Record):
    group_id: Text
    any_of: Annotated[list[Text], Field(min_length=1)]


class ExpectedTurn(Record):
    turn_id: Text
    checkpoints: Annotated[list[Text], Field(min_length=2)]
    evidence_required: bool
    evidence_groups: Annotated[list[EvidenceGroup], Field(min_length=1)]
    forbidden_claims: Annotated[list[Text], Field(min_length=1)]
    expected_touchpoints: list[Text]
    review_note: Text


class Expectation(Record):
    case_id: Text
    turns: Annotated[list[ExpectedTurn], Field(min_length=1)]


class Evidence(Record):
    evidence_id: Text
    source_id: Text
    file_path: Text
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    source_text: Text
    file_sha256: Sha256


class Source(Record):
    source_id: Text
    repo_url: Annotated[str, StringConstraints(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    commit_sha: GitSha
    tree_sha: GitSha


class Manifest(Record):
    dataset_id: Text
    version: Literal[1]
    split: Literal["development"]
    sources: Annotated[list[Source], Field(min_length=1)]
    case_count: Annotated[int, Field(ge=1)]
    turn_count: Annotated[int, Field(ge=1)]
    category_counts: dict[Category, Annotated[int, Field(ge=1)]]
    payload_sha256: dict[str, Sha256]


class RubricDimension(Record):
    dimension: Dimension
    weight: Annotated[int, Field(ge=1, le=100)]
    anchors: dict[Literal["0", "1", "2"], Text]


class Rubric(Record):
    rubric_id: Text
    dimensions: list[RubricDimension]
    minimum_passing_ratings: dict[Dimension, Rating]
    fatal_errors: dict[str, Text]
    case_pass_rule: Literal["all_turns_pass"]
    pending_policy: Literal["withhold_final_metrics"]
    failure_policy: Literal["zero_in_fixed_denominator"]
    notes: Annotated[list[Text], Field(min_length=1)]

    @model_validator(mode="after")
    def complete(self):
        if Counter(item.dimension for item in self.dimensions) != Counter(DIMENSIONS):
            raise ValueError("Each scoring dimension must appear exactly once")
        if sum(item.weight for item in self.dimensions) != 100:
            raise ValueError("Scoring weights must sum to 100")
        if set(self.minimum_passing_ratings) != set(DIMENSIONS):
            raise ValueError("Passing rules must cover every dimension")
        if any(set(item.anchors) != {"0", "1", "2"} for item in self.dimensions):
            raise ValueError("Each dimension needs all three rating anchors")
        if not self.fatal_errors:
            raise ValueError("Fatal error definitions are required")
        return self


class Ratings(Record):
    correctness: Rating
    evidence: Rating
    completeness: Rating
    boundaries: Rating


class Review(Record):
    case_id: Text
    turn_id: Text
    status: Literal["not_run", "execution_failed", "pending_review", "reviewed"]
    ratings: Ratings | None
    fatal_errors: list[Text]
    output_sha256: Sha256 | None
    reviewer: Text | None
    rationale: str
    error: Text | None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "reviewed":
            if self.ratings is None or self.output_sha256 is None or not self.reviewer or not self.rationale.strip() or self.error:
                raise ValueError("Reviewed turns need ratings, output hash, reviewer and rationale")
        elif self.ratings is not None or self.fatal_errors or self.reviewer is not None or self.rationale:
            raise ValueError("Unreviewed turns cannot contain human scores or review conclusions")
        if self.status in {"not_run", "execution_failed"} and self.output_sha256 is not None:
            raise ValueError("Uncompleted turns cannot claim a completed output")
        if self.status == "pending_review" and self.output_sha256 is None:
            raise ValueError("Pending review needs a completed output hash")
        if (self.status == "execution_failed") != (self.error is not None):
            raise ValueError("Only execution failures have an execution error")
        return self


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(data):
    return json.loads(data, object_pairs_hook=_object)


def read_rows(data, schema):
    lines = data.decode("utf-8").splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError("JSONL must contain records without blank lines")
    return [schema.model_validate(read_json(line)) for line in lines]


def _unique(rows, key):
    values = [getattr(row, key) for row in rows]
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {key}")
    return dict(zip(values, rows))


def _blob(commit, path):
    relative = PurePosixPath(path)
    if (relative.is_absolute() or PureWindowsPath(path).drive or "\\" in path or relative.as_posix() != path
            or any(part in {".", ".."} or part.lower() == ".git" for part in relative.parts)
            or any(ord(char) < 32 for char in path)):
        raise ValueError(f"Invalid source path: {path}")
    blob = commit.tree / path
    if blob.type != "blob" or blob.mode not in (0o100644, 0o100755):
        raise ValueError(f"Evidence must be a regular committed file: {path}")
    return blob.data_stream.read()


class Bundle:
    def __init__(self, manifest, cases, expectations, evidence, rubric, reviews, fingerprint):
        self.manifest, self.cases, self.expectations = manifest, cases, expectations
        self.evidence, self.rubric, self.reviews, self.fingerprint = evidence, rubric, reviews, fingerprint

    def model_inputs(self):
        """Only user inputs and source identity; never return reviewer-only gold."""
        sources = {source.source_id: source for source in self.manifest.sources}
        return [{"case_id": case.case_id, "turns": [turn.model_dump() for turn in case.turns],
                 "source": sources[case.source_id].model_dump()} for case in self.cases]


def validate_bundle(directory, repository):
    root = Path(directory)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = Manifest.model_validate(read_json(manifest_bytes))
    if set(manifest.payload_sha256) != set(PAYLOADS):
        raise ValueError("Manifest payload inventory does not match the fixed format")
    payloads = {name: (root / name).read_bytes() for name in PAYLOADS}
    if any(digest(payloads[name]) != manifest.payload_sha256[name] for name in PAYLOADS):
        raise ValueError("Dataset payload checksum mismatch")
    cases = read_rows(payloads["cases.jsonl"], Case)
    expectations = read_rows(payloads["expectations.jsonl"], Expectation)
    evidence = read_rows(payloads["evidence.jsonl"], Evidence)
    reviews = read_rows(payloads["review-template.jsonl"], Review)
    rubric = Rubric.model_validate(read_json(payloads["rubric.json"]))
    case_map, gold_map = _unique(cases, "case_id"), _unique(expectations, "case_id")
    source_map, evidence_map = _unique(manifest.sources, "source_id"), _unique(evidence, "evidence_id")
    if set(case_map) != set(gold_map):
        raise ValueError("Inputs and expectations must contain identical case IDs")
    if (len(cases), sum(len(case.turns) for case in cases)) != (manifest.case_count, manifest.turn_count):
        raise ValueError("Case/turn count mismatch")
    if Counter(case.category for case in cases) != manifest.category_counts or set(manifest.category_counts) != set(Category.__args__):
        raise ValueError("Category coverage/count mismatch")
    used = set()
    for case in cases:
        if case.source_id not in source_map:
            raise ValueError("Unknown case source")
        turn_ids = [turn.turn_id for turn in case.turns]
        if turn_ids != [f"T{number}" for number in range(1, len(turn_ids) + 1)]:
            raise ValueError("Turn IDs must be unique and sequential")
        if turn_ids != [turn.turn_id for turn in gold_map[case.case_id].turns]:
            raise ValueError("Expected turns must match input turns in order")
        for turn in gold_map[case.case_id].turns:
            _unique(turn.evidence_groups, "group_id")
            for group in turn.evidence_groups:
                if len(set(group.any_of)) != len(group.any_of):
                    raise ValueError("Duplicate alternative evidence ID")
                for identity in group.any_of:
                    item = evidence_map.get(identity)
                    if item is None or item.source_id != case.source_id:
                        raise ValueError("Unknown or cross-version evidence")
                    used.add(identity)
    if used != set(evidence_map):
        raise ValueError("Unused evidence records are not allowed")
    expected_keys = {(case.case_id, turn.turn_id) for case in cases for turn in case.turns}
    if len(reviews) != len(expected_keys) or {(row.case_id, row.turn_id) for row in reviews} != expected_keys:
        raise ValueError("Review template must cover every turn exactly once")
    if any(row.status != "not_run" for row in reviews):
        raise ValueError("The fixed review template must remain unexecuted and unscored")
    with Repo(repository) as repo:
        origin = repo.remotes.origin.url.removesuffix(".git").rstrip("/")
        commits = {}
        for source in manifest.sources:
            if source.repo_url != origin:
                raise ValueError("Git origin does not match source repository")
            commit = repo.commit(source.commit_sha)
            if commit.hexsha != source.commit_sha or commit.tree.hexsha != source.tree_sha:
                raise ValueError("Pinned source identity mismatch")
            commits[source.source_id] = commit
        for item in evidence:
            data = _blob(commits[item.source_id], item.file_path)
            lines = data.decode("utf-8").splitlines()
            if not item.start_line <= item.end_line <= len(lines):
                raise ValueError("Evidence line range is outside the source")
            if digest(data) != item.file_sha256 or "\n".join(lines[item.start_line - 1:item.end_line]) != item.source_text:
                raise ValueError(f"Evidence content mismatch: {item.evidence_id}")
        for case in cases:
            for turn in gold_map[case.case_id].turns:
                for path in turn.expected_touchpoints:
                    _blob(commits[case.source_id], path)
    return Bundle(manifest, cases, expectations, evidence, rubric, reviews, digest(manifest_bytes))
