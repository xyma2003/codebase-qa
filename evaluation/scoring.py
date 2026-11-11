"""Calculate explicit human ratings; never infer correctness from keywords."""

from evaluation.dataset import DIMENSIONS, Review


def summarize_reviews(bundle, records):
    reviews = [Review.model_validate(row) if isinstance(row, dict) else row for row in records]
    expected = {(case.case_id, turn.turn_id) for case in bundle.cases for turn in case.turns}
    indexed = {(row.case_id, row.turn_id): row for row in reviews}
    if len(indexed) != len(reviews) or set(indexed) != expected:
        raise ValueError("Reviews must cover the fixed case/turn denominator exactly once")
    weights = {item.dimension: item.weight for item in bundle.rubric.dimensions}
    results = {}
    for key, row in indexed.items():
        if any(error not in bundle.rubric.fatal_errors for error in row.fatal_errors):
            raise ValueError("Unknown fatal error code")
        if row.status in {"not_run", "pending_review"}:
            results[key] = {"score": None, "passed": None}
        elif row.status == "execution_failed":
            results[key] = {"score": 0, "passed": False}
        else:
            ratings = row.ratings.model_dump()
            score = sum(weights[key] * ratings[key] / 2 for key in DIMENSIONS)
            passed = all(ratings[key] >= threshold for key, threshold in bundle.rubric.minimum_passing_ratings.items())
            results[key] = {"score": 0 if row.fatal_errors else score, "passed": passed and not row.fatal_errors}
    cases = []
    for case in bundle.cases:
        turns = [results[(case.case_id, turn.turn_id)] for turn in case.turns]
        complete = all(turn["score"] is not None for turn in turns)
        cases.append({"case_id": case.case_id, "category": case.category,
                      "score": sum(turn["score"] for turn in turns) / len(turns) if complete else None,
                      "passed": all(turn["passed"] for turn in turns) if complete else None})
    finalized = all(case["score"] is not None for case in cases)
    return {
        "dataset_id": bundle.manifest.dataset_id, "dataset_fingerprint": bundle.fingerprint,
        "required_cases": len(cases), "required_turns": len(expected),
        "completed_turns": sum(row.status in {"pending_review", "reviewed"} for row in reviews),
        "reviewed_turns": sum(row.status == "reviewed" for row in reviews),
        "execution_failed_turns": sum(row.status == "execution_failed" for row in reviews),
        "finalized": finalized,
        "mean_score": sum(case["score"] for case in cases) / len(cases) if finalized else None,
        "pass_rate": sum(case["passed"] for case in cases) / len(cases) if finalized else None,
        "cases": cases,
    }
