"""
Compares what Clausurus and Microsoft Presidio hide, on two datasets:

  swiss       the 40 synthetic Swiss texts in eval/data (this repo)
  ai4privacy  a seeded random sample of the ai4privacy/pii-masking-300k validation
              split (English, German, French, Italian), downloaded at run time

Systems:
  clausurus-rules    Clausurus regex + checksum recognizers only
  clausurus-apertus  Clausurus rules + Apertus (needs APERTUS_API_KEY)
  presidio           Presidio AnalyzerEngine, spaCy *_lg models, predefined
                     recognizers enabled for all four languages; reported at
                     score >= 0 (everything returned) and score >= 0.5

Scoring ignores labels: every system's spans are merged into "hidden regions".
  caught         a gold span overlapped by any hidden region (1 - leak rate)
  fully covered  every non-space character of the gold span is hidden
  precision      hidden regions that overlap some gold span / all hidden regions
Numbers are pooled over spans; 95% intervals come from a bootstrap over documents.

The ai4privacy data is licensed for academic / non-commercial use only and may not
be redistributed. This script downloads it to a local cache outside the repository
and writes aggregate numbers only (no dataset text) to eval/benchmark_results.md.

Usage:
    python benchmark.py --systems clausurus-rules presidio
    APERTUS_API_KEY=... python benchmark.py --per-language 50
"""

import argparse
import asyncio
import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("clausurus", HERE.parent / "clausurus.py")
clausurus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clausurus)

AI4P_FILES = {
    "en": "1english_openpii_8k.jsonl",
    "de": "german_openpii_8k.jsonl",
    "fr": "french_openpii_8k.jsonl",
    "it": "italian_openpiii_8k.jsonl",
}
AI4P_URL = "https://huggingface.co/datasets/ai4privacy/pii-masking-300k/resolve/main/data/validation/"

AI4P_GROUPS = {
    "Names": ["GIVENNAME1", "GIVENNAME2", "LASTNAME1", "LASTNAME2", "LASTNAME3"],
    "Email & phone": ["EMAIL", "TEL"],
    "Government IDs": ["SOCIALNUMBER", "IDCARD", "PASSPORT", "DRIVERLICENSE"],
    "Street address": ["STREET", "BUILDING", "SECADDRESS", "POSTCODE"],
    "City / state / country": ["CITY", "STATE", "COUNTRY"],
    "IP & username": ["IP", "USERNAME"],
    "Passwords": ["PASS"],
    "Birth date": ["BOD"],
    "Other dates & times": ["DATE", "TIME"],
    "Sex & title": ["SEX", "TITLE"],
    "Geo coordinates": ["GEOCOORD"],
    "Card issuer": ["CARDISSUER"],
}
DIRECT_GROUPS = [
    "Names", "Email & phone", "Government IDs", "Street address",
    "IP & username", "Passwords", "Birth date", "Geo coordinates",
]
LABEL_TO_GROUP = {label: group for group, labels in AI4P_GROUPS.items() for label in labels}


# --------------------------------------------------------------------------
# Datasets: list of {"id", "lang", "text", "gold": [(start, end, group)]}
# --------------------------------------------------------------------------


def load_swiss() -> list[dict]:
    data_dir = HERE / "data"
    docs = []
    for doc_id in json.loads((data_dir / "manifest.json").read_text()):
        meta = json.loads((data_dir / f"{doc_id}.json").read_text(encoding="utf-8"))
        docs.append(
            {
                "id": doc_id,
                "lang": meta["lang"],
                "text": (data_dir / f"{doc_id}.txt").read_text(encoding="utf-8"),
                "gold": [(e["start"], e["end"], e["type"]) for e in meta["entities"]],
            }
        )
    return docs


def load_ai4privacy(cache: Path, per_language: int, seed: int) -> list[dict]:
    cache.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    docs = []
    for lang, name in AI4P_FILES.items():
        path = cache / name
        if not path.exists():
            print(f"Downloading {name} to {cache} ...", file=sys.stderr)
            with httpx.stream("GET", AI4P_URL + name, follow_redirects=True, timeout=300) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        rows = [json.loads(line) for line in path.open(encoding="utf-8")]
        for row in rng.sample(rows, per_language):
            gold = [(m["start"], m["end"], LABEL_TO_GROUP.get(m["label"], m["label"])) for m in row["privacy_mask"]]
            docs.append({"id": str(row["id"]), "lang": lang, "text": row["source_text"], "gold": gold})
    return docs


# --------------------------------------------------------------------------
# Systems: each returns a list of (start, end) spans it would hide
# --------------------------------------------------------------------------


def clausurus_rules(doc) -> list[tuple]:
    return [(e.start, e.end) for e in clausurus.rule_based_entities(doc["text"])]


class ClausurusApertus:
    def __init__(self, key, base, model, concurrency=3):
        self.key, self.base, self.model = key, base, model
        self.sem = asyncio.Semaphore(concurrency)
        self.errors = 0

    async def __call__(self, client, doc) -> list[tuple]:
        async with self.sem:
            ents, err = await clausurus.detect_entities(
                doc["text"], True, client, self.base, self.key, self.model, 60
            )
        if err:
            self.errors += 1
        return [(e.start, e.end) for e in ents]


class Presidio:
    MODELS = {"en": "en_core_web_lg", "de": "de_core_news_lg", "fr": "fr_core_news_lg", "it": "it_core_news_lg"}

    def __init__(self):
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        langs = list(self.MODELS)
        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": l, "model_name": m} for l, m in self.MODELS.items()],
            }
        ).create_engine()
        registry = RecognizerRegistry(supported_languages=langs)
        registry.load_predefined_recognizers(languages=langs, nlp_engine=nlp)
        self.engine = AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=langs)

    def recognizers_by_language(self) -> dict:
        return {l: sorted({r.name for r in self.engine.get_recognizers(language=l)}) for l in self.MODELS}

    def __call__(self, doc) -> list[tuple]:
        return [(r.start, r.end, r.score) for r in self.engine.analyze(text=doc["text"], language=doc["lang"])]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def merge(spans: list[tuple]) -> list[tuple]:
    out = []
    for s, e in sorted((s, e) for s, e, *_ in spans if e > s):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def score_doc(text: str, gold: list[tuple], predicted: list[tuple]) -> dict:
    regions = merge(predicted)
    hidden = [False] * len(text)
    for s, e in regions:
        for i in range(s, min(e, len(text))):
            hidden[i] = True
    per_group = {}
    for s, e, group in gold:
        caught = any(rs < e and s < re for rs, re in regions)
        chars = [i for i in range(s, e) if not text[i].isspace()]
        full = bool(chars) and all(hidden[i] for i in chars)
        g = per_group.setdefault(group, [0, 0, 0])
        g[0] += 1
        g[1] += caught
        g[2] += full
    correct = sum(1 for rs, re in regions if any(rs < e and s < re for s, e, _ in gold))
    return {"groups": per_group, "regions": len(regions), "correct_regions": correct}


def pool(scores: list[dict], groups=None) -> dict:
    total = caught = full = regions = correct = 0
    for sc in scores:
        for g, (t, c, f) in sc["groups"].items():
            if groups is None or g in groups:
                total, caught, full = total + t, caught + c, full + f
        regions += sc["regions"]
        correct += sc["correct_regions"]
    return {
        "total": total,
        "caught": caught / total if total else float("nan"),
        "full": full / total if total else float("nan"),
        "leaked": total - caught,
        "precision": correct / regions if regions else float("nan"),
    }


def bootstrap(scores: list[dict], key: str, groups=None, n=1000, seed=0) -> tuple:
    rng = random.Random(seed)
    values = sorted(pool([rng.choice(scores) for _ in scores], groups)[key] for _ in range(n))
    return values[int(0.025 * n)], values[int(0.975 * n)]


# --------------------------------------------------------------------------
# Runner and report
# --------------------------------------------------------------------------


async def run_system(name, docs, fn, is_async=False, client=None):
    t0 = time.perf_counter()
    if is_async:
        preds = await asyncio.gather(*(fn(client, d) for d in docs))
    else:
        preds = [fn(d) for d in docs]
    elapsed = time.perf_counter() - t0
    print(f"  {name}: {len(docs)} docs in {elapsed:.1f}s", file=sys.stderr)
    return preds, elapsed / max(len(docs), 1)


def fmt_pct(x):
    return "n/a" if x != x else f"{100 * x:.1f}%"


def fmt_ci(ci):
    return f"{100 * ci[0]:.0f}–{100 * ci[1]:.0f}%"


def report_dataset(title, docs, results, group_order, direct_groups) -> list[str]:
    lines = [f"## {title}", ""]
    n_gold = sum(len(d["gold"]) for d in docs)
    lines.append(f"{len(docs)} documents, {n_gold} labelled spans.")
    lines.append("")
    headline_groups = direct_groups
    label = "direct identifiers" if direct_groups else "all labelled spans"
    lines.append(f"### Headline: {label}")
    lines.append("")
    lines.append("| System | Caught (95% CI) | Leaked | Fully covered | Precision (95% CI) | Hidden regions | Time / doc |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, (scores, per_doc) in results.items():
        p = pool(scores, headline_groups)
        lines.append(
            f"| {name} | {fmt_pct(p['caught'])} ({fmt_ci(bootstrap(scores, 'caught', headline_groups))}) "
            f"| {p['leaked']} / {p['total']} | {fmt_pct(p['full'])} "
            f"| {fmt_pct(p['precision'])} ({fmt_ci(bootstrap(scores, 'precision', headline_groups))}) "
            f"| {sum(s['regions'] for s in scores)} | {per_doc * 1000:.0f} ms |"
        )
    lines.append("")
    if direct_groups:
        p_all = {name: pool(scores) for name, (scores, _) in results.items()}
        lines.append("All labelled spans, including quasi-identifiers (city, dates, times, sex, title):")
        lines.append("")
        lines.append("| System | Caught | Fully covered |")
        lines.append("|---|---|---|")
        for name, p in p_all.items():
            lines.append(f"| {name} | {fmt_pct(p['caught'])} | {fmt_pct(p['full'])} |")
        lines.append("")
    lines.append("### Caught, per category")
    lines.append("")
    lines.append("| Category | Spans | " + " | ".join(results) + " |")
    lines.append("|---|---|" + "---|" * len(results))
    for group in group_order:
        totals = [pool(scores, [group]) for scores, _ in results.values()]
        if not totals[0]["total"]:
            continue
        mark = "" if not direct_groups or group in direct_groups else " *"
        lines.append(
            f"| {group}{mark} | {totals[0]['total']} | " + " | ".join(fmt_pct(t["caught"]) for t in totals) + " |"
        )
    if direct_groups:
        lines.append("")
        lines.append("\\* quasi-identifier, not counted in the headline.")
    lines.append("")
    lines.append("### Caught, per language (headline spans)")
    lines.append("")
    lines.append("| Language | " + " | ".join(results) + " |")
    lines.append("|---|" + "---|" * len(results))
    for lang in sorted({d["lang"] for d in docs}):
        idx = [i for i, d in enumerate(docs) if d["lang"] == lang]
        cells = [fmt_pct(pool([scores[i] for i in idx], headline_groups)["caught"]) for scores, _ in results.values()]
        lines.append(f"| {lang} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


async def main_async(args):
    datasets = {}
    if "swiss" in args.datasets:
        datasets["swiss"] = load_swiss()
    if "ai4privacy" in args.datasets:
        datasets["ai4privacy"] = load_ai4privacy(Path(args.cache).expanduser(), args.per_language, args.seed)

    presidio = Presidio() if "presidio" in args.systems else None
    apertus_key = os.environ.get("APERTUS_API_KEY", "")
    apertus = None
    if "clausurus-apertus" in args.systems:
        if not apertus_key:
            sys.exit("clausurus-apertus needs APERTUS_API_KEY")
        apertus = ClausurusApertus(apertus_key, args.apertus_base, args.apertus_model, args.concurrency)

    out = [
        "# Clausurus vs. Microsoft Presidio",
        "",
        "Generated by `eval/benchmark.py`. Aggregate numbers only. See the end of this file for setup and caveats.",
        "",
    ]
    async with httpx.AsyncClient() as client:
        for ds_name, docs in datasets.items():
            print(f"{ds_name}: {len(docs)} docs", file=sys.stderr)
            results = {}
            for system in args.systems:
                if system == "clausurus-rules":
                    preds, t = await run_system(system, docs, clausurus_rules)
                    results[system] = ([score_doc(d["text"], d["gold"], p) for d, p in zip(docs, preds)], t)
                elif system == "clausurus-apertus":
                    apertus.errors = 0
                    preds, t = await run_system(system, docs, apertus, is_async=True, client=client)
                    results[system] = ([score_doc(d["text"], d["gold"], p) for d, p in zip(docs, preds)], t)
                    if apertus.errors:
                        print(f"  Apertus failed on {apertus.errors} docs (rules only there)", file=sys.stderr)
                        results[system + f" ({apertus.errors} Apertus errors)"] = results.pop(system)
                elif system == "presidio":
                    preds, t = await run_system(system, docs, presidio)
                    for threshold in (0.0, 0.5):
                        kept = [[s for s in p if s[2] >= threshold] for p in preds]
                        results[f"presidio (score ≥ {threshold})"] = (
                            [score_doc(d["text"], d["gold"], p) for d, p in zip(docs, kept)],
                            t,
                        )
            if ds_name == "swiss":
                order = sorted({g for d in docs for _, _, g in d["gold"]})
                out += report_dataset("Swiss synthetic set (this repo)", docs, results, order, None)
            else:
                out += report_dataset(
                    f"ai4privacy/pii-masking-300k, validation split, {args.per_language} random documents "
                    f"per language (seed {args.seed})",
                    docs,
                    results,
                    list(AI4P_GROUPS),
                    DIRECT_GROUPS,
                )

    out += [
        "## Setup and caveats",
        "",
        "- Scoring ignores labels: a system gets credit for hiding a span, whatever it calls it.",
        "- Presidio: `presidio-analyzer` with spaCy `en_core_web_lg`, `de_core_news_lg`, `fr_core_news_lg`, "
        "`it_core_news_lg`, and its predefined recognizers loaded for all four languages. No custom recognizers "
        "were added; a team using Presidio would normally add some for their own ID formats.",
        *(
            [
                "- Presidio recognizers active per language: "
                + "; ".join(f"{l}: {len(r)}" for l, r in presidio.recognizers_by_language().items())
                + " (Presidio enables some recognizers only for some languages, e.g. US and Italian ID formats)."
            ]
            if presidio
            else []
        ),
        f"- Apertus: `{args.apertus_model}` at the Swiss AI Weeks endpoint, temperature 0, same prompt as the "
        "function. Its answers are not fully deterministic, so repeated runs differ slightly.",
        "- ai4privacy texts are synthetic and generated by an LLM; many are form-like or XML records rather than "
        "letters. Their label set includes items Clausurus is not designed to hide (dates, times, sex, titles).",
        "- ai4privacy data is used under its academic / non-commercial license, downloaded at run time and not "
        "stored in this repository. Dataset: ai4privacy, *pii-masking-300k* (2024), "
        "https://huggingface.co/datasets/ai4privacy/pii-masking-300k.",
        "- Neither system was tuned on the ai4privacy data before this run.",
    ]
    report = "\n".join(out) + "\n"
    path = HERE / args.output
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Written to {path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", nargs="+", default=["swiss", "ai4privacy"], choices=["swiss", "ai4privacy"])
    parser.add_argument(
        "--systems",
        nargs="+",
        default=["clausurus-rules", "clausurus-apertus", "presidio"],
        choices=["clausurus-rules", "clausurus-apertus", "presidio"],
    )
    parser.add_argument("--per-language", type=int, default=50)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--cache", default="~/.cache/clausurus/ai4privacy")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--apertus-base", default="https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1"
    )
    parser.add_argument("--apertus-model", default="swiss-ai/Apertus-v1.5-70B")
    parser.add_argument("--output", default="benchmark_results.md")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
