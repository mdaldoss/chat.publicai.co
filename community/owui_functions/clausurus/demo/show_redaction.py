"""
Prints the original letter next to what Clausurus would actually send to an
external LLM provider, side by side. Meant for slides / a live demo, not for
production use.

Usage:
    python show_redaction.py [path/to/letter.txt]
    (defaults to letter_de.txt in this directory)

Requires APERTUS_API_KEY in the environment (or set --no-apertus for a
rules-only run). Never hardcode a key here -- load it from demo/.env
(gitignored) or export it in your shell.
"""

import argparse
import asyncio
import importlib.util
import os
import sys
import textwrap
from pathlib import Path

CLAUSURUS_PATH = Path(__file__).resolve().parents[1] / "clausurus.py"
spec = importlib.util.spec_from_file_location("clausurus", CLAUSURUS_PATH)
clausurus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clausurus)

import httpx


def wrap_block(text: str, width: int = 58) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width=width) or [""])
    return lines


def side_by_side(left: str, right: str, left_title: str, right_title: str):
    left_lines = wrap_block(left)
    right_lines = wrap_block(right)
    width = 58
    print(f"{left_title:<{width}} | {right_title}")
    print("-" * width + "-+-" + "-" * width)
    for l, r in zip(left_lines + [""] * (len(right_lines) - len(left_lines)),
                     right_lines + [""] * (len(left_lines) - len(right_lines))):
        print(f"{l:<{width}} | {r}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", default=str(Path(__file__).parent / "letter_de.txt"))
    parser.add_argument("--no-apertus", action="store_true", help="rules-only, no Apertus call")
    parser.add_argument(
        "--apertus-base",
        default=os.environ.get(
            "APERTUS_API_BASE", "https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1"
        ),
    )
    parser.add_argument("--apertus-model", default=os.environ.get("APERTUS_MODEL", "swiss-ai/Apertus-v1.5-70B"))
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    apertus_key = os.environ.get("APERTUS_API_KEY", "")

    use_apertus = not args.no_apertus
    if use_apertus and not apertus_key:
        print("No APERTUS_API_KEY set; falling back to --no-apertus (rules only).", file=sys.stderr)
        use_apertus = False

    async with httpx.AsyncClient() as client:
        entities, err = await clausurus.detect_entities(
            text, use_apertus, client, args.apertus_base, apertus_key, args.apertus_model, 30.0
        )
        if err:
            print(f"Apertus error (falling back to whatever rules found): {err}", file=sys.stderr)

    anonymizer = clausurus.Anonymizer()
    redacted = anonymizer.anonymize(text, entities)

    side_by_side(text, redacted, "ORIGINAL (stays on your device)", "SENT TO THE EXTERNAL PROVIDER")

    counts: dict[str, int] = {}
    for e in entities:
        counts[e.type] = counts.get(e.type, 0) + 1
    print("\nRedaction summary:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none")


if __name__ == "__main__":
    asyncio.run(main())
