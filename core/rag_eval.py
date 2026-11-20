"""Measure retrieval and answer quality against a labelled TXT corpus."""

import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from core.rag import DocumentIndex
from core.rag_answer import answer_question, rerank_passages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="JSON list of question and filename pairs")
    parser.add_argument("--db", type=Path, default=Path("run_artifacts/rag"))
    parser.add_argument("--model", action="append", default=[])
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list) or not cases or any(
        not isinstance(case, dict)
        or not isinstance(case.get("question"), str)
        or not case["question"].strip()
        or "filename" not in case
        or (case["filename"] is not None and not isinstance(case["filename"], str))
        for case in cases
    ):
        parser.error("Her soru için question ve filename girin. Kaynak yoksa filename null olmalı.")
    known = [case for case in cases if case["filename"] is not None]
    if not known:
        parser.error("Arama ölçümü için en az bir kaynaklı soru girin.")

    store = DocumentIndex(args.db)
    if not store.collection.count():
        parser.error("Değerlendirmeden önce TXT belgelerini indeksleyin.")
    hit_at_3 = rerank_top_1 = 0
    for case in known:
        question, expected = case["question"], case["filename"]
        candidates = store.search(question, top_k=6)
        hit_at_3 += any(passage["filename"] == expected for passage in candidates[:3])
        ranked = rerank_passages(question, candidates)
        rerank_top_1 += bool(ranked and ranked[0]["filename"] == expected)
    print(json.dumps({
        "label_granularity": "filename",
        "hit_at_3": {"correct": hit_at_3, "total": len(known)},
        "rerank_top_1": {"correct": rerank_top_1, "total": len(known)},
    }), flush=True)

    for model in args.model:
        timings = []
        correct_sources = correct_abstentions = errors = 0
        for case in cases:
            start = perf_counter()
            try:
                response = answer_question(store, case["question"], 6, 3, model)
            except RuntimeError as error:
                errors += 1
                print(json.dumps({"model": model, "error": str(error)}), flush=True)
                continue
            timings.append(perf_counter() - start)
            cited = {source["filename"] for source in response["sources"].values()}
            expected = case["filename"]
            if expected is None:
                correct_abstentions += not cited
            else:
                correct_sources += cited == {expected}
        print(json.dumps({
            "model": model,
            "correct_sources": {"correct": correct_sources, "total": len(known)},
            "correct_abstentions": {
                "correct": correct_abstentions, "total": len(cases) - len(known)
            },
            "median_seconds": round(median(timings), 2) if timings else None,
            "errors": errors,
        }), flush=True)


if __name__ == "__main__":
    main()
