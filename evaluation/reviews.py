"""Editable review packets bound to a sealed run, never to a filename alone."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from evaluation.archive import verify_archive, write_json, write_rows
from evaluation.dataset import Record, Review, Sha256, Text, digest, read_json, read_rows, validate_bundle


class Submission(Record):
    format_version: Literal[1]
    review_id: Text
    purpose: Literal["human", "simulation"]
    source_run_id: Text
    source_archive_sha256: Sha256
    dataset_fingerprint: Sha256
    rubric_sha256: Sha256


def load_source(archive, repository):
    root = Path(archive)
    receipt = verify_archive(root, repository)
    bundle = validate_bundle(root / "dataset", repository)
    run = read_json((root / "run.json").read_bytes())
    rows = read_rows((root / "reviews.jsonl").read_bytes(), Review)
    return root, receipt, bundle, run, rows


def check_submission(header, receipt, bundle, run, root):
    expected = {
        "source_run_id": run["run_id"], "source_archive_sha256": receipt["archive_sha256"],
        "dataset_fingerprint": bundle.fingerprint,
        "rubric_sha256": digest((root / "dataset/rubric.json").read_bytes()),
    }
    if any(getattr(header, key) != value for key, value in expected.items()):
        raise ValueError("Review submission belongs to another run, dataset or rubric")


def review_context(root, bundle):
    """A reviewer sees complete turns, including earlier turns and fixed gold."""
    gold = {item.case_id: item for item in bundle.expectations}
    cases = []
    for case_number, case in enumerate(bundle.cases, 1):
        turns = []
        for turn_number, turn in enumerate(case.turns, 1):
            folder = root / "turns" / f"{case_number:03d}-{turn_number:02d}"
            output = folder / "output.json"
            turns.append({"turn": turn.model_dump(), "expected": gold[case.case_id].turns[turn_number-1].model_dump(),
                          "outcome": read_json((folder / "outcome.json").read_bytes()),
                          "output_sha256": digest(output.read_bytes()) if output.exists() else None,
                          "checked_answer": read_json(output.read_bytes()) if output.exists() else None,
                          "error": read_json((folder / "error.json").read_bytes()) if (folder / "error.json").exists() else None})
        cases.append({"case_id": case.case_id, "category": case.category, "source_id": case.source_id, "turns": turns})
    return {"sources": [source.model_dump() for source in bundle.manifest.sources],
            "evidence": [item.model_dump() for item in bundle.evidence], "cases": cases}


def prepare_review(archive, repository, output, *, purpose="human"):
    root, receipt, bundle, run, rows = load_source(archive, repository)
    header = Submission.model_validate({"format_version": 1, "review_id": str(uuid4()), "purpose": purpose,
        "source_run_id": run["run_id"], "source_archive_sha256": receipt["archive_sha256"],
        "dataset_fingerprint": bundle.fingerprint, "rubric_sha256": digest((root / "dataset/rubric.json").read_bytes())})
    context = review_context(root, bundle)
    destination = Path(output).resolve()
    if destination.is_relative_to(root.resolve()):
        raise ValueError("Review packet must be outside the sealed source archive")
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "review.json", header.model_dump())
    write_rows(destination / "reviews.jsonl", [row.model_dump() for row in rows])
    write_json(destination / "context.json", context)
    (destination / "rubric.json").write_bytes((root / "dataset/rubric.json").read_bytes())
    return {"status": "review_packet_prepared", "review_id": header.review_id, "purpose": purpose,
            "eligible_turns": sum(row.status == "pending_review" for row in rows), "required_turns": len(rows)}


def merge_reviews(base, submitted, rubric):
    indexed = {(row.case_id, row.turn_id): row for row in base}
    seen = set()
    for row in submitted:
        key = (row.case_id, row.turn_id)
        if key in seen or key not in indexed:
            raise ValueError("Duplicate or unknown review case/turn")
        seen.add(key)
        original = indexed[key]
        if original.status != "pending_review":
            if row != original:
                raise ValueError("Cannot review or rewrite an uncompleted execution")
        else:
            if row.status not in {"pending_review", "reviewed"} or row.output_sha256 != original.output_sha256:
                raise ValueError("Review is bound to another output or changes execution state")
            if any(code not in rubric.fatal_errors for code in row.fatal_errors):
                raise ValueError("Unknown fatal error code")
            indexed[key] = row
    return [indexed[(row.case_id, row.turn_id)] for row in base]


def load_packet(packet, source):
    root, receipt, bundle, run, base = source
    packet = Path(packet)
    header = Submission.model_validate(read_json((packet / "review.json").read_bytes()))
    check_submission(header, receipt, bundle, run, root)
    if read_json((packet / "context.json").read_bytes()) != review_context(root, bundle):
        raise ValueError("Reviewer context differs from original outputs or frozen expectations")
    if (packet / "rubric.json").read_bytes() != (root / "dataset/rubric.json").read_bytes():
        raise ValueError("Review packet changed the frozen rubric")
    submitted = read_rows((packet / "reviews.jsonl").read_bytes(), Review)
    merged = merge_reviews(base, submitted, bundle.rubric)
    return header, merged
