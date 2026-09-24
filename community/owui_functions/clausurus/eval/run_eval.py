"""
Evaluates Clausurus' entity detection against the synthetic gold dataset in
eval/data/, for two configurations:
  (a) rules only
  (b) rules + Apertus (entity/contextual-identifier detection)

For each configuration it reports per-entity-type precision/recall (overlap
match: a predicted span counts as a hit if it overlaps a gold span of the
same type) and, separately, the "leak rate": how many gold spans have NO
overlapping prediction of any type (i.e. would reach the external LLM
unredacted) — this is the number that matters for the threat model, since a
CONTEXTUAL span mislabelled as PERSON is still fully redacted.

Usage:
    python run_eval.py                       # rules only + rules+Apertus (needs APERTUS_API_KEY)
    python run_eval.py --rules-only           # skip Apertus entirely (no network/API key needed)
    python run_eval.py --apertus-base URL --apertus-key KEY --apertus-model MODEL

Writes eval/results.md.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # import clausurus.py

import importlib.util

spec = importlib.util.spec_from_file_location("clausurus", Path(__file__).resolve().parents[1] / "clausurus.py")
clausurus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clausurus)

import httpx

DATA_DIR = Path(__file__).parent / "data"


def load_dataset():
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    docs = []
    for doc_id in manifest:
        text = (DATA_DIR / f"{doc_id}.txt").read_text(encoding="utf-8")
        gold = json.loads((DATA_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
        docs.append((doc_id, text, gold["entities"]))
    return docs


def spans_overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def score(predictions: list[clausurus.Entity], gold: list[dict]):
    """Returns (per_type_stats, leaked_gold_spans)."""
    per_type = {}  # type -> {tp, fp, fn}
    matched_gold_idx = set()
    matched_pred_idx = set()

    for gi, g in enumerate(gold):
        for pi, p in enumerate(predictions):
            if pi in matched_pred_idx:
                continue
            if spans_overlap(g["start"], g["end"], p.start, p.end) and g["type"] == p.type:
                matched_gold_idx.add(gi)
                matched_pred_idx.add(pi)
                break

    for gi, g in enumerate(gold):
        stats = per_type.setdefault(g["type"], {"tp": 0, "fp": 0, "fn": 0})
        if gi in matched_gold_idx:
            stats["tp"] += 1
        else:
            stats["fn"] += 1

    for pi, p in enumerate(predictions):
        if pi not in matched_pred_idx:
            stats = per_type.setdefault(p.type, {"tp": 0, "fp": 0, "fn": 0})
            stats["fp"] += 1

    # "Leaked" = gold span with NO overlapping prediction of ANY type (worst case
    # for the threat model: this exact text reaches the external provider).
    leaked = []
    for gi, g in enumerate(gold):
        if any(spans_overlap(g["start"], g["end"], p.start, p.end) for p in predictions):
            continue
        leaked.append(g)

    return per_type, leaked


def aggregate(all_per_type: list[dict]):
    total = {}
    for per_type in all_per_type:
        for etype, stats in per_type.items():
            agg = total.setdefault(etype, {"tp": 0, "fp": 0, "fn": 0})
            for k in ("tp", "fp", "fn"):
                agg[k] += stats[k]
    return total


def precision_recall(stats: dict):
    tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    return precision, recall


async def run_config(docs, use_apertus: bool, apertus_base, apertus_key, apertus_model, apertus_timeout):
    all_per_type = []
    all_leaked = []
    per_doc_leak_count = []
    errors = 0
    latencies = []

    async with httpx.AsyncClient() as client:
        for doc_id, text, gold in docs:
            t0 = time.perf_counter()
            entities, err = await clausurus.detect_entities(
                text, use_apertus, client, apertus_base, apertus_key, apertus_model, apertus_timeout
            )
            latencies.append(time.perf_counter() - t0)
            if err:
                errors += 1
            per_type, leaked = score(entities, gold)
            all_per_type.append(per_type)
            all_leaked.append((doc_id, leaked))
            per_doc_leak_count.append(len(leaked))

    return {
        "aggregate": aggregate(all_per_type),
        "leaked_by_doc": all_leaked,
        "total_leaks": sum(per_doc_leak_count),
        "total_gold": sum(len(g) for _, _, g in docs),
        "apertus_errors": errors,
        "avg_latency_s": sum(latencies) / len(latencies) if latencies else 0,
    }


def format_report(rules_only, rules_apertus, docs) -> str:
    lines = ["# Clausurus evaluation results\n"]
    lines.append(f"Dataset: {len(docs)} synthetic texts (DE/FR/IT/EN), "
                  f"{rules_only['total_gold']} gold entity spans.\n")

    def table(result, title):
        out = [f"## {title}\n", "| Type | TP | FP | FN | Precision | Recall |",
               "|---|---|---|---|---|---|"]
        for etype in sorted(result["aggregate"]):
            stats = result["aggregate"][etype]
            p, r = precision_recall(stats)
            out.append(
                f"| {etype} | {stats['tp']} | {stats['fp']} | {stats['fn']} | "
                f"{p:.2f} | {r:.2f} |"
            )
        out.append("")
        out.append(f"**Leaked identifiers (no overlapping redaction of any type): "
                    f"{result['total_leaks']} / {result['total_gold']}**\n")
        out.append(f"Avg. detection latency: {result['avg_latency_s']*1000:.0f} ms/doc")
        if result["apertus_errors"]:
            out.append(f"\n⚠️ Apertus errors during this run: {result['apertus_errors']} "
                        f"(those docs fell back to rules-only for this run).")
        return "\n".join(out)

    lines.append(table(rules_only, "(a) Rules only"))
    lines.append("")
    if rules_apertus is not None:
        lines.append(table(rules_apertus, "(b) Rules + Apertus"))
        lines.append("")
        lines.append("## Per-document leaks, rules + Apertus\n")
        any_leak = False
        for doc_id, leaked in rules_apertus["leaked_by_doc"]:
            if leaked:
                any_leak = True
                spans = ", ".join(f"`{s['text']}` ({s['type']})" for s in leaked)
                lines.append(f"- **{doc_id}**: {spans}")
        if not any_leak:
            lines.append("- None. Every gold span was covered by at least one predicted span.")
    else:
        lines.append("(b) skipped: run without --rules-only and with an Apertus key/base to include it.")

    lines.append("\n---\n_Generated by eval/run_eval.py. This is Clausurus' own eval, on Clausurus' own "
                  "synthetic dataset — treat the numbers as a development-time signal, not an "
                  "independent audit._")
    return "\n".join(lines)


async def main_async(args):
    docs = load_dataset()
    rules_only = await run_config(docs, False, "", "", "", 0)

    rules_apertus = None
    if not args.rules_only:
        key = args.apertus_key or os.environ.get("APERTUS_API_KEY", "")
        if not key:
            print("No Apertus key given (--apertus-key or APERTUS_API_KEY); skipping (b). "
                  "Pass --rules-only to silence this.", file=sys.stderr)
        else:
            rules_apertus = await run_config(
                docs, True, args.apertus_base, key, args.apertus_model, args.apertus_timeout
            )

    report = format_report(rules_only, rules_apertus, docs)
    out_path = Path(__file__).parent / "results.md"
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nWritten to {out_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--apertus-base", default="https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1")
    parser.add_argument("--apertus-key", default="")
    parser.add_argument("--apertus-model", default="swiss-ai/Apertus-v1.5-70B")
    parser.add_argument("--apertus-timeout", type=float, default=30.0)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
