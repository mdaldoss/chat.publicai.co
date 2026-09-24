"""
title: Clausurus
author: Public AI community
version: 0.1.0
license: MIT
description: >
    Anonymizes personal data (regex + entity recognition via Apertus)
    before sending a request to an external, bring-your-own-key LLM
    provider (OpenAI, Gemini, Anthropic or OpenRouter), then restores
    the real values in the streamed reply.

    Clausurus does NOT guarantee anonymity. It removes direct
    identifiers it can find; contextual re-identification remains
    possible, and pseudonymized text can still be personal data under
    the Swiss FADP / GDPR. See README.md for the full threat model.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

import httpx
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Checksum validators
# --------------------------------------------------------------------------


def validate_ahv_checksum(digits: str) -> bool:
    """Swiss AHV/AVS number (756.XXXX.XXXX.XX), EAN-13 style check digit."""
    if len(digits) != 13 or not digits.isdigit():
        return False
    nums = [int(d) for d in digits]
    body, check = nums[:12], nums[12]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(body))
    computed = (10 - (total % 10)) % 10
    return computed == check


def validate_iban_checksum(iban: str) -> bool:
    """ISO 7064 mod-97-10, as used by IBAN."""
    iban = iban.replace(" ", "").upper()
    if not (15 <= len(iban) <= 34):
        return False
    if not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    try:
        converted = "".join(str(int(c, 36)) for c in rearranged)
    except ValueError:
        return False
    return int(converted) % 97 == 1


def validate_uid_checksum(digits: str) -> bool:
    """Swiss UID/CHE number (CHE-XXX.XXX.XXX), modulo-11 check digit."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4]
    nums = [int(d) for d in digits]
    body, check = nums[:8], nums[8]
    total = sum(w * d for w, d in zip(weights, body))
    computed = 11 - (total % 11)
    if computed == 11:
        computed = 0
    if computed == 10:
        return False  # 10 is not a valid UID check digit
    return computed == check


def validate_luhn(digits: str) -> bool:
    """Luhn checksum, used by payment card numbers."""
    if not digits.isdigit():
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# --------------------------------------------------------------------------
# Entities & rule-based recognizers
# --------------------------------------------------------------------------


@dataclass
class Entity:
    start: int
    end: int
    text: str
    type: str
    source: str  # "rule" | "apertus"
    confidence: float = 1.0


HONORIFICS = (
    r"(?:Herr|Frau|Hr\.|Fr\.|Dr\.|Prof\.|"  # DE
    r"M\.|Mme|Mlle|"  # FR
    r"Sig\.|Sig\.ra|Sig\.na|"  # IT
    r"Mr\.|Mrs\.|Ms\.|Miss|Dr)"
)
CAPWORD = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+"

# Each recognizer: (type, compiled_regex, optional checksum_validator(raw_digits)->bool)
RULE_PATTERNS: list[tuple[str, re.Pattern, Optional[callable]]] = [
    (
        "AHV",
        re.compile(r"\b756\.\d{4}\.\d{4}\.\d{2}\b"),
        lambda m: validate_ahv_checksum(re.sub(r"\D", "", m)),
    ),
    (
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b"),
        lambda m: validate_iban_checksum(m),
    ),
    (
        "UID",
        re.compile(r"\bCHE[- ]?\d{3}\.?\d{3}\.?\d{3}\b", re.IGNORECASE),
        lambda m: validate_uid_checksum(re.sub(r"\D", "", m)),
    ),
    (
        "CARD",
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        lambda m: validate_luhn(re.sub(r"\D", "", m)),
    ),
    (
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        None,
    ),
    (
        "PHONE",
        re.compile(r"(?<!\d)(?:\+41|0041|0)\s?\d{2}(?:[ .-]?\d{2,3}){2,3}(?!\d)"),
        None,
    ),
    (
        "IP",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        None,
    ),
    (
        "PLATE",
        re.compile(
            r"\b(?:AG|AI|AR|BE|BL|BS|FR|GE|GL|GR|JU|LU|NE|NW|OW|SG|SH|SO|SZ|TG|TI|UR|VD|VS|ZG|ZH)"
            r"[ ]?\d{1,6}\b"
        ),
        None,
    ),
    (
        "ADDRESS",
        re.compile(
            r"\b(?:"
            # German/Swiss-German: street name glued to a suffix, e.g. "Seestrasse 63"
            r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]*(?:strasse|straße|str\.|weg|gasse|platz)\s?\d{1,4}[a-z]?"
            r"|"
            # French: prefix word + (article) + name, e.g. "Rue du Lac 18", "Avenue de la Gare 113"
            r"(?:Rue|Route|Chemin|Avenue|Boulevard|All[ée]e)\s+"
            r"(?:de\s+la\s+|de\s+l['’]|du\s+|des\s+)?"
            r"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:\s+[A-ZÀ-ÖØ-Þ][\w'’-]*){0,2}\s+\d{1,4}[a-z]?"
            r"|"
            # Italian: prefix word + (article) + name, e.g. "Via del Lago 170", "Piazza del Mercato"
            r"(?:Via|Piazza|Corso|Viale)\s+"
            r"(?:del(?:la|lo)?\s+|dei\s+|degli\s+)?"
            r"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:\s+[A-ZÀ-ÖØ-Þ][\w'’-]*){0,2}\s+\d{1,4}[a-z]?"
            r"|"
            # English: name + suffix word, e.g. "Station Avenue 38", "Church Close 105"
            r"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:\s+[A-ZÀ-ÖØ-Þ][\w'’-]*)?\s+"
            r"(?:Street|Road|Avenue|Lane|Close|Drive|Way|Boulevard|Place|Court|Terrace)\s+\d{1,4}[a-z]?"
            r")"
            r"(?:[ \t]*,?[ \t]*\d{4}[ \t]+[A-ZÀ-ÖØ-Þ][A-Za-zà-öø-ÿ'’ -]+)?"
        ),
        None,
    ),
    (
        "POSTAL",
        re.compile(r"\b\d{4}[ \t]+[A-ZÀ-ÖØ-Þ][A-Za-zà-öø-ÿ'’-]+(?:[ -][A-ZÀ-ÖØ-Þ][A-Za-zà-öø-ÿ'’-]+)?\b"),
        None,
    ),
    (
        "PERSON",
        re.compile(rf"\b{HONORIFICS}[ \t]+(?:{CAPWORD}[ \t]+){{0,2}}{CAPWORD}\b"),
        None,
    ),
]


def rule_based_entities(text: str) -> list[Entity]:
    entities: list[Entity] = []
    for etype, pattern, validator in RULE_PATTERNS:
        for m in pattern.finditer(text):
            raw = m.group(0)
            if validator is not None and not validator(raw):
                continue
            entities.append(Entity(m.start(), m.end(), raw, etype, "rule"))

    # A standalone POSTAL match that's fully inside an ADDRESS match (e.g. the
    # ", 1000 Faketown" tail already captured by the ADDRESS pattern) is a
    # redundant, not a missed, finding -- both get redacted at merge time
    # regardless, so drop the contained duplicate to keep entity counts honest.
    addresses = [e for e in entities if e.type == "ADDRESS"]
    entities = [
        e
        for e in entities
        if not (e.type == "POSTAL" and any(a.start <= e.start and e.end <= a.end for a in addresses))
    ]
    return entities


# --------------------------------------------------------------------------
# Apertus-based entity & contextual-identifier recognizer
# --------------------------------------------------------------------------

APERTUS_SYSTEM_PROMPT = """You are a privacy analyst. Read the message below and list every \
span of text that could identify a specific natural person, either directly or through \
context. Include:
- PERSON: full or partial personal names
- LOCATION: addresses, place names, specific buildings
- ORG: employers, schools, clubs that narrow down who someone is
- CONTEXTUAL: a description that singles out one identifiable person even without a \
name, e.g. "the only pharmacist in the village", "the mayor's wife", "my neighbour at \
number 12"

Do not include generic terms, job titles alone, or common nouns. Only include text that \
verbatim appears in the message. Reply with ONLY a JSON object of this exact shape, no \
markdown, no commentary:
{"entities": [{"text": "<verbatim excerpt from the message>", "type": "PERSON|LOCATION|ORG|CONTEXTUAL"}]}
If there is nothing to report, reply {"entities": []}."""


def _retry_delay_seconds(resp: httpx.Response, attempt: int) -> float:
    for header in ("Retry-After", "X-Ratelimit-Reset", "X-RateLimit-Reset"):
        value = resp.headers.get(header)
        if not value:
            continue
        match = re.match(r"([\d.]+)", value)
        if match:
            return float(match.group(1))
    return min(2.0**attempt, 8.0)


async def apertus_entities(
    client: httpx.AsyncClient,
    api_base: str,
    api_key: str,
    model: str,
    text: str,
    timeout: float,
    max_retries: int = 3,
) -> list[Entity]:
    """Ask Apertus to find entities/contextual identifiers, then map the
    verbatim strings it returns back onto spans in the original text.
    Anything Apertus returns that does NOT appear verbatim in the text is
    discarded (defends against hallucination and prompt injection via the
    model output). Retries with backoff on 429/5xx, honoring
    Retry-After/X-Ratelimit-Reset when present."""
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": APERTUS_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    resp = None
    for attempt in range(max_retries + 1):
        resp = await client.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code not in (429, 500, 502, 503, 504) or attempt == max_retries:
            break
        await asyncio.sleep(_retry_delay_seconds(resp, attempt))
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    # Be forgiving of stray markdown fences.
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    entities: list[Entity] = []
    for item in data.get("entities", []):
        span_text = item.get("text", "")
        etype = item.get("type", "CONTEXTUAL")
        if not span_text or span_text not in text:
            continue  # hallucinated / not grounded in the source text
        for m in re.finditer(re.escape(span_text), text):
            entities.append(Entity(m.start(), m.end(), span_text, etype, "apertus", 0.8))
    return entities


# --------------------------------------------------------------------------
# Merge + placeholder engine
# --------------------------------------------------------------------------

CONTEXTUAL_LABELS = {
    # crude, language-agnostic-ish description generator used as the placeholder
    # body for CONTEXTUAL spans so the model still has *some* usable context.
    "CONTEXTUAL": "a specific local individual",
}


def merge_entities(entities: list[Entity]) -> list[Entity]:
    """Sort by start, keep the longest span at each overlap, preferring
    rule-based matches (higher precision) over Apertus matches when they
    overlap exactly or one contains the other."""
    if not entities:
        return []
    ordered = sorted(entities, key=lambda e: (e.start, -(e.end - e.start), e.source != "rule"))
    merged: list[Entity] = []
    for ent in ordered:
        if merged and ent.start < merged[-1].end:
            prev = merged[-1]
            prev_len = prev.end - prev.start
            cur_len = ent.end - ent.start
            better = cur_len > prev_len or (cur_len == prev_len and ent.source == "rule" and prev.source != "rule")
            if better:
                merged[-1] = ent
            continue
        merged.append(ent)
    return merged


class Anonymizer:
    """Deterministic placeholder numbering, stable within one pipe() call and
    reusable across turns of the same request (re-derived on every call, not
    persisted, to stay stateless across replicas)."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.value_to_placeholder: dict[str, str] = {}
        self.placeholder_to_value: dict[str, str] = {}

    def _placeholder_for(self, entity: Entity) -> str:
        key = f"{entity.type}:{entity.text.lower()}"
        if key in self.value_to_placeholder:
            return self.value_to_placeholder[key]
        self.counters[entity.type] = self.counters.get(entity.type, 0) + 1
        n = self.counters[entity.type]
        if entity.type in CONTEXTUAL_LABELS:
            placeholder = f"[CONTEXT_{n}: {CONTEXTUAL_LABELS[entity.type]}]"
        else:
            placeholder = f"[{entity.type}_{n}]"
        self.value_to_placeholder[key] = placeholder
        self.placeholder_to_value[placeholder] = entity.text
        return placeholder

    def anonymize(self, text: str, entities: list[Entity]) -> str:
        merged = merge_entities(entities)
        out = []
        cursor = 0
        for ent in merged:
            if ent.start < cursor:
                continue
            out.append(text[cursor:ent.start])
            out.append(self._placeholder_for(ent))
            cursor = ent.end
        out.append(text[cursor:])
        return "".join(out)

    def restore(self, text: str) -> str:
        for placeholder, value in self.placeholder_to_value.items():
            text = text.replace(placeholder, value)
        return text


class StreamDeAnonymizer:
    """Buffers streamed text so a placeholder like '[PERSON_1]' split across
    two chunks is still restored correctly."""

    def __init__(self, mapping: dict[str, str], max_hold: int = 80):
        self.mapping = mapping
        self.buffer = ""
        self.max_hold = max_hold

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        out = []
        while True:
            idx = self.buffer.find("[")
            if idx == -1:
                out.append(self.buffer)
                self.buffer = ""
                break
            out.append(self.buffer[:idx])
            self.buffer = self.buffer[idx:]
            end = self.buffer.find("]")
            if end == -1:
                if len(self.buffer) > self.max_hold:
                    out.append(self.buffer[0])
                    self.buffer = self.buffer[1:]
                    continue
                break
            token = self.buffer[: end + 1]
            out.append(self.mapping.get(token, token))
            self.buffer = self.buffer[end + 1 :]
        return "".join(out)

    def flush(self) -> str:
        rest = self.buffer
        self.buffer = ""
        return rest


async def detect_entities(
    text: str,
    use_apertus: bool,
    apertus_client: Optional[httpx.AsyncClient],
    apertus_base: str,
    apertus_key: str,
    apertus_model: str,
    apertus_timeout: float,
) -> tuple[list[Entity], Optional[str]]:
    """Returns (entities, apertus_error). apertus_error is None on success,
    or a short description of what went wrong so the caller can decide
    whether to fall back or block."""
    entities = rule_based_entities(text)
    if not use_apertus:
        return entities, None
    try:
        apertus_ents = await apertus_entities(
            apertus_client, apertus_base, apertus_key, apertus_model, text, apertus_timeout
        )
        return entities + apertus_ents, None
    except Exception as e:  # noqa: BLE001 - defend against any provider failure
        return entities, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Provider adapters (OpenAI-compatible + native Anthropic)
# --------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "kind": "openai",
    },
    "gemini": {
        "label": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "kind": "openai",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-3-5-sonnet-latest",
        "kind": "anthropic",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "kind": "openai",
    },
}


async def stream_openai_compatible(
    client: httpx.AsyncClient, base_url: str, api_key: str, model: str, messages: list[dict]
) -> AsyncGenerator[str, None]:
    payload = {"model": model, "messages": messages, "stream": True}
    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    ) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            raise RuntimeError(f"Provider returned {resp.status_code}: {body.decode(errors='replace')[:500]}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = obj.get("choices", [{}])[0].get("delta", {})
            piece = delta.get("content")
            if piece:
                yield piece


async def stream_anthropic(
    client: httpx.AsyncClient, base_url: str, api_key: str, model: str, messages: list[dict]
) -> AsyncGenerator[str, None]:
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    chat_messages = [
        {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
    ]
    payload = {
        "model": model,
        "max_tokens": 4096,
        "stream": True,
        "messages": chat_messages,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    ) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            raise RuntimeError(f"Provider returned {resp.status_code}: {body.decode(errors='replace')[:500]}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data:
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content_block_delta":
                piece = obj.get("delta", {}).get("text")
                if piece:
                    yield piece


# --------------------------------------------------------------------------
# The Open WebUI Pipe
# --------------------------------------------------------------------------


class Filter:
    class Valves(BaseModel):
        # Apertus (used internally, only for PII/entity detection)
        apertus_api_base: str = Field(
            default="https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1",
            description="OpenAI-compatible Apertus endpoint used only to detect entities/PII. "
            "Point this at any Apertus deployment (e.g. Public AI's own).",
        )
        apertus_api_key: str = Field(
            default="", description="API key for the Apertus endpoint above. Set via env/admin, never committed."
        )
        apertus_model: str = Field(default="swiss-ai/Apertus-v1.5-70B")
        apertus_timeout_seconds: float = Field(default=20.0)
        enable_apertus: bool = Field(
            default=True, description="Use Apertus to catch entities and contextual identifiers regex can't."
        )
        block_on_apertus_failure: bool = Field(
            default=True,
            description="If Apertus is unreachable: True = block the request (safer); "
            "False = send with regex-only redaction and a visible warning.",
        )
        require_confirmation: bool = Field(
            default=False,
            description="Ask the user to confirm the redacted text via a dialog before it is sent.",
        )
        confirmation_timeout_seconds: float = Field(default=60.0)

        # Admin-provided fallback keys (leave empty in production; demo only).
        openai_api_key: str = Field(default="")
        gemini_api_key: str = Field(default="")
        anthropic_api_key: str = Field(default="")
        openrouter_api_key: str = Field(default="")

        openai_model: str = Field(default=PROVIDER_DEFAULTS["openai"]["default_model"])
        gemini_model: str = Field(default=PROVIDER_DEFAULTS["gemini"]["default_model"])
        anthropic_model: str = Field(default=PROVIDER_DEFAULTS["anthropic"]["default_model"])
        openrouter_model: str = Field(default=PROVIDER_DEFAULTS["openrouter"]["default_model"])

    class UserValves(BaseModel):
        openai_api_key: str = Field(default="", description="Your own OpenAI key (bring-your-own-key).")
        gemini_api_key: str = Field(default="", description="Your own Gemini key.")
        anthropic_api_key: str = Field(default="", description="Your own Anthropic key.")
        openrouter_api_key: str = Field(default="", description="Your own OpenRouter key.")

    def __init__(self):
        self.valves = self.Valves()
        self.icon = "🔒"

    def pipes(self) -> list[dict]:
        return [{"id": key, "name": f"Clausurus 🔒 · {cfg['label']}"} for key, cfg in PROVIDER_DEFAULTS.items()]

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ):
        provider_key = (body.get("model") or "").split(".", 1)[-1]
        if provider_key not in PROVIDER_DEFAULTS:
            yield f"Clausurus: unknown provider '{provider_key}'."
            return
        cfg = PROVIDER_DEFAULTS[provider_key]

        user_valves = None
        if __user__ is not None:
            user_valves = __user__.get("valves")
        api_key = getattr(user_valves, f"{provider_key}_api_key", "") or getattr(
            self.valves, f"{provider_key}_api_key", ""
        )
        if not api_key:
            yield (
                f"Clausurus: no API key configured for {cfg['label']}. "
                f"Add your own key in the Clausurus user settings (bring your own key)."
            )
            return
        model = getattr(self.valves, f"{provider_key}_model") or cfg["default_model"]

        messages = body.get("messages", [])

        async with httpx.AsyncClient() as client:
            anonymizer = Anonymizer()
            redaction_counts: dict[str, int] = {}
            any_apertus_error: Optional[str] = None

            anonymized_messages = []
            for msg in messages:
                content = msg.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    anonymized_messages.append(msg)
                    continue

                entities, apertus_error = await detect_entities(
                    content,
                    self.valves.enable_apertus,
                    client,
                    self.valves.apertus_api_base,
                    self.valves.apertus_api_key,
                    self.valves.apertus_model,
                    self.valves.apertus_timeout_seconds,
                )
                if apertus_error:
                    any_apertus_error = apertus_error

                for ent in entities:
                    redaction_counts[ent.type] = redaction_counts.get(ent.type, 0) + 1

                redacted = anonymizer.anonymize(content, entities)
                anonymized_messages.append({**msg, "content": redacted})

            if any_apertus_error and self.valves.enable_apertus:
                if self.valves.block_on_apertus_failure:
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"🔒 Clausurus blocked: Apertus unreachable ({any_apertus_error})",
                                    "done": True,
                                    "hidden": False,
                                },
                            }
                        )
                    yield (
                        "Clausurus blocked this request: the Apertus entity-detection service "
                        "is unreachable, and Clausurus is configured to fail closed rather than "
                        "send text with reduced redaction. An admin can change this in the valves."
                    )
                    return
                else:
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": (
                                        "⚠️ Clausurus: Apertus unavailable, sent with regex-only redaction"
                                    ),
                                    "done": False,
                                    "hidden": False,
                                },
                            }
                        )

            total = sum(redaction_counts.values())
            summary = ", ".join(f"{v} {k}" for k, v in sorted(redaction_counts.items()))
            status_text = f"🔒 Clausurus: {total} identifier(s) redacted" + (f" ({summary})" if summary else "")
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": status_text, "done": total == 0, "hidden": False}}
                )

            if self.valves.require_confirmation and total > 0 and __event_call__:
                preview = anonymized_messages[-1].get("content", "") if anonymized_messages else ""
                response = await __event_call__(
                    {
                        "type": "confirmation",
                        "data": {
                            "title": "Clausurus: confirm redacted text before sending",
                            "message": preview[:4000],
                        },
                    }
                )
                # __event_call__ resolves to a plain bool (True = confirmed, False =
                # cancelled), or {"error": ...} if the client session timed out/disconnected.
                if response is not True:
                    yield "Clausurus: request cancelled (confirmation was declined or timed out)."
                    return

            deanon = StreamDeAnonymizer(anonymizer.placeholder_to_value)
            try:
                if cfg["kind"] == "anthropic":
                    stream = stream_anthropic(client, cfg["base_url"], api_key, model, anonymized_messages)
                else:
                    base_url = cfg["base_url"]
                    stream = stream_openai_compatible(client, base_url, api_key, model, anonymized_messages)

                async for piece in stream:
                    out = deanon.feed(piece)
                    if out:
                        yield out
                tail = deanon.flush()
                if tail:
                    yield tail
            except Exception as e:  # noqa: BLE001
                yield f"\n\nClausurus: request to {cfg['label']} failed: {e}"
