"""
title: Clausurus
author: Public AI community
version: 0.2.0
license: MIT
description: Hides personal data (rules + Apertus) before a message reaches an external bring-your-own-key LLM, lets you review what is hidden, and restores it in the reply. Not an anonymity guarantee; see README.md.
"""

import asyncio
import hashlib
import html
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
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
    return (10 - (total % 10)) % 10 == check


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
    total = sum(w * d for w, d in zip(weights, nums[:8]))
    computed = 11 - (total % 11)
    if computed == 11:
        computed = 0
    if computed == 10:
        return False  # 10 is not a valid UID check digit
    return computed == nums[8]


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
    source: str  # "rule" | "apertus" | "user"
    confidence: float = 1.0


def normalize_key(text: str) -> str:
    """Key used to recognise the same value across messages and in review decisions."""
    return " ".join(text.split()).lower()


HONORIFICS = (
    r"(?:Herr|Frau|Hr\.|Fr\.|Dr\.|Prof\.|"  # DE
    r"M\.|Mme|Mlle|"  # FR
    r"Sig\.|Sig\.ra|Sig\.na|"  # IT
    r"Mr\.|Mrs\.|Ms\.|Miss|Dr)"
)
CAPWORD = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+"
TOWN = rf"{CAPWORD}(?:[ -]{CAPWORD})?"
SP = r"[ \t]"  # whitespace that never crosses a line break
HONORIFIC_PREFIX = re.compile(rf"{HONORIFICS}{SP}+")

# Each recognizer: (type, compiled_regex, optional validator(matched_text) -> bool)
RULE_PATTERNS: list[tuple[str, re.Pattern, Optional[callable]]] = [
    ("AHV", re.compile(r"\b756\.\d{4}\.\d{4}\.\d{2}\b"), lambda m: validate_ahv_checksum(re.sub(r"\D", "", m))),
    (
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b"),
        validate_iban_checksum,
    ),
    (
        "UID",
        re.compile(r"\bCHE[- ]?\d{3}\.?\d{3}\.?\d{3}\b", re.IGNORECASE),
        lambda m: validate_uid_checksum(re.sub(r"\D", "", m)),
    ),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), lambda m: validate_luhn(re.sub(r"\D", "", m))),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    ("PHONE", re.compile(rf"(?<!\d)(?:\+41|0041|0){SP}?\d{{2}}(?:[ .-]?\d{{2,3}}){{2,3}}(?!\d)"), None),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), None),
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
            rf"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]*(?:strasse|straße|str\.|weg|gasse|platz){SP}?\d{{1,4}}[a-z]?"
            r"|"
            # French: prefix word + (article) + name, e.g. "Rue du Lac 18", "Avenue de la Gare 113"
            rf"(?:Rue|Route|Chemin|Avenue|Boulevard|All[ée]e){SP}+"
            rf"(?:de{SP}+la{SP}+|de{SP}+l['’]|du{SP}+|des{SP}+)?"
            rf"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:{SP}+[A-ZÀ-ÖØ-Þ][\w'’-]*){{0,2}}{SP}+\d{{1,4}}[a-z]?"
            r"|"
            # Italian: prefix word + (article) + name, e.g. "Via del Lago 170"
            rf"(?:Via|Piazza|Corso|Viale){SP}+"
            rf"(?:del(?:la|lo)?{SP}+|dei{SP}+|degli{SP}+)?"
            rf"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:{SP}+[A-ZÀ-ÖØ-Þ][\w'’-]*){{0,2}}{SP}+\d{{1,4}}[a-z]?"
            r"|"
            # English: name + suffix word, e.g. "Station Avenue 38", "Church Close 105"
            rf"[A-ZÀ-ÖØ-Þ][\w'’-]*(?:{SP}+[A-ZÀ-ÖØ-Þ][\w'’-]*)?{SP}+"
            rf"(?:Street|Road|Avenue|Lane|Close|Drive|Way|Boulevard|Place|Court|Terrace){SP}+\d{{1,4}}[a-z]?"
            r")"
            # optional ", 8000 Zürich" tail on the same line
            rf"(?:{SP}*,?{SP}*\d{{4}}{SP}+{TOWN})?"
        ),
        None,
    ),
    ("POSTAL", re.compile(rf"\b\d{{4}}{SP}+{TOWN}\b"), None),
    # The honorific anchors the match but stays visible ("Frau [PERSON_1]"), so the reply
    # doesn't come back as "Frau Frau Heidi ...".
    ("PERSON", re.compile(rf"\b{HONORIFICS}{SP}+(?P<value>(?:{CAPWORD}{SP}+){{0,2}}{CAPWORD})\b"), None),
]


def rule_based_entities(text: str) -> list[Entity]:
    entities: list[Entity] = []
    for etype, pattern, validator in RULE_PATTERNS:
        for m in pattern.finditer(text):
            group = "value" if "value" in pattern.groupindex else 0
            raw = m.group(group)
            if validator is not None and not validator(raw):
                continue
            entities.append(Entity(m.start(group), m.end(group), raw, etype, "rule"))

    # A POSTAL match inside an ADDRESS match (the ", 1000 Faketown" tail) is a
    # duplicate finding, not a separate one; drop it so counts stay honest.
    addresses = [e for e in entities if e.type == "ADDRESS"]
    return [
        e
        for e in entities
        if not (e.type == "POSTAL" and any(a.start <= e.start and e.end <= a.end for a in addresses))
    ]


def term_entities(text: str, terms: list[str]) -> list[Entity]:
    """Occurrences (case-insensitive) of terms the user asked to hide."""
    entities = []
    for term in terms:
        if not term:
            continue
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
            entities.append(Entity(m.start(), m.end(), m.group(0), "CUSTOM", "user"))
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
name. Return the whole phrase, including articles and qualifiers, e.g. "der einzige \
Tierarzt im Tal", "la directrice de l'école du village", "il parroco della frazione", \
"the only female firefighter in town", "my landlord's son".

The message may be in German, French, Italian, English or Swiss German; the same rules \
apply. Do not include generic terms, job titles on their own, or common nouns. Copy each \
span exactly as it appears in the message, same spelling and capitalisation. Reply with \
ONLY a JSON object of this exact shape, no markdown, no commentary:
{"entities": [{"text": "<verbatim excerpt from the message>", "type": "PERSON|LOCATION|ORG|CONTEXTUAL"}]}
If there is nothing to report, reply {"entities": []}."""

APERTUS_TYPES = {"PERSON", "LOCATION", "ORG", "CONTEXTUAL"}
MIN_APERTUS_SPAN = 3  # ignore 1-2 character "entities"; they would redact every occurrence

# In-process cache of Apertus results, keyed by a hash of (model, text). Earlier turns
# of a conversation are re-scanned on every request; this avoids paying for them again.
# A cache miss (restart, other replica) just calls Apertus again.
_APERTUS_CACHE: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
_APERTUS_CACHE_SIZE = 512


def _retry_delay_seconds(resp: httpx.Response, attempt: int) -> float:
    for header in ("Retry-After", "X-Ratelimit-Reset", "X-RateLimit-Reset"):
        value = resp.headers.get(header)
        if not value:
            continue
        match = re.match(r"([\d.]+)", value)
        if match:
            return min(float(match.group(1)), 30.0)
    return min(2.0**attempt, 8.0)


def _parse_apertus_json(content: str) -> list[tuple[str, str]]:
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
    found = []
    for item in data.get("entities", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        span_text = str(item.get("text", ""))
        etype = str(item.get("type", "CONTEXTUAL")).upper()
        found.append((span_text, etype if etype in APERTUS_TYPES else "CONTEXTUAL"))
    return found


async def apertus_entities(
    client: httpx.AsyncClient,
    api_base: str,
    api_key: str,
    model: str,
    text: str,
    timeout: float,
    max_retries: int = 3,
) -> list[Entity]:
    """Ask Apertus which spans identify a person, then map the strings it returns back
    onto the original text. Anything not found verbatim in the text is discarded, so a
    hallucinated or injected answer can't add text; it can only fail to find things
    (which the rules layer and the user review partly cover)."""
    cache_key = hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()
    found = _APERTUS_CACHE.get(cache_key)
    if found is not None:
        _APERTUS_CACHE.move_to_end(cache_key)
    else:
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
        found = _parse_apertus_json(resp.json()["choices"][0]["message"]["content"])
        _APERTUS_CACHE[cache_key] = found
        while len(_APERTUS_CACHE) > _APERTUS_CACHE_SIZE:
            _APERTUS_CACHE.popitem(last=False)

    entities: list[Entity] = []
    for span_text, etype in found:
        if len(span_text.strip()) < MIN_APERTUS_SPAN or span_text not in text:
            continue
        # Keep "Frau"/"M."/"Mr." outside the placeholder, as the rules do.
        prefix = HONORIFIC_PREFIX.match(span_text) if etype == "PERSON" else None
        skip = prefix.end() if prefix and prefix.end() < len(span_text) else 0
        for m in re.finditer(re.escape(span_text), text):
            entities.append(Entity(m.start() + skip, m.end(), span_text[skip:], etype, "apertus", 0.8))
    return entities


async def detect_entities(
    text: str,
    use_apertus: bool,
    apertus_client: Optional[httpx.AsyncClient],
    apertus_base: str,
    apertus_key: str,
    apertus_model: str,
    apertus_timeout: float,
) -> tuple[list[Entity], Optional[str]]:
    """Returns (entities, apertus_error). apertus_error is None on success, or a short
    description so the caller can decide whether to block or fall back."""
    entities = rule_based_entities(text)
    if not use_apertus:
        return entities, None
    try:
        found = await apertus_entities(
            apertus_client, apertus_base, apertus_key, apertus_model, text, apertus_timeout
        )
        return entities + found, None
    except Exception as e:  # noqa: BLE001 - any provider failure must be handled by policy
        return entities, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Merge + placeholder engine
# --------------------------------------------------------------------------

SOURCE_RANK = {"rule": 0, "user": 1, "apertus": 2}


def merge_entities(entities: list[Entity], text: str) -> list[Entity]:
    """Collapse overlapping spans into their union, so no part of any detected span is
    left unredacted. The merged span keeps the type of its most reliable member
    (rules > user > Apertus, then the longest)."""
    if not entities:
        return []
    ordered = sorted(entities, key=lambda e: (e.start, -(e.end - e.start)))
    merged: list[Entity] = []
    for ent in ordered:
        if merged and ent.start < merged[-1].end:
            prev = merged[-1]
            better = (SOURCE_RANK.get(ent.source, 9), -(ent.end - ent.start)) < (
                SOURCE_RANK.get(prev.source, 9),
                -(prev.end - prev.start),
            )
            lead = ent if better else prev
            end = max(prev.end, ent.end)
            merged[-1] = Entity(prev.start, end, text[prev.start : end], lead.type, lead.source, lead.confidence)
            continue
        merged.append(Entity(ent.start, ent.end, ent.text, ent.type, ent.source, ent.confidence))
    return merged


class Anonymizer:
    """Numbers placeholders per type in order of first appearance. A fresh instance is
    built on every request over the whole conversation, so numbering is stable across
    turns and across replicas without storing anything."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.value_to_placeholder: dict[str, str] = {}
        self.placeholder_to_value: dict[str, str] = {}
        self.placeholder_types: dict[str, str] = {}

    def placeholder_for(self, entity: Entity) -> str:
        key = f"{entity.type}:{normalize_key(entity.text)}"
        if key in self.value_to_placeholder:
            return self.value_to_placeholder[key]
        self.counters[entity.type] = self.counters.get(entity.type, 0) + 1
        n = self.counters[entity.type]
        if entity.type == "CONTEXTUAL":
            placeholder = f"[CONTEXT_{n}: a specific local individual]"
        else:
            placeholder = f"[{entity.type}_{n}]"
        self.value_to_placeholder[key] = placeholder
        self.placeholder_to_value[placeholder] = entity.text
        self.placeholder_types[placeholder] = entity.type
        return placeholder

    def anonymize(self, text: str, entities: list[Entity]) -> str:
        out = []
        cursor = 0
        for ent in merge_entities(entities, text):
            out.append(text[cursor : ent.start])
            out.append(self.placeholder_for(ent))
            cursor = ent.end
        out.append(text[cursor:])
        return "".join(out)

    def restore(self, text: str) -> str:
        for placeholder, value in self.placeholder_to_value.items():
            text = text.replace(placeholder, value)
        return text


class StreamDeAnonymizer:
    """Buffers streamed text so a placeholder like '[PERSON_1]' split across two chunks
    is still restored."""

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


# --------------------------------------------------------------------------
# Conversation handling: text parts, findings, review decisions
# --------------------------------------------------------------------------


@dataclass
class TextPart:
    message_index: int
    part_index: Optional[int]  # None when the message content is a plain string
    role: str
    text: str
    entities: list  # merged entities after detection


def split_messages(messages: list[dict]) -> tuple[list[TextPart], int]:
    """Collect every text part of the conversation. Returns (parts, non_text_count):
    images and other non-text parts can't be redacted, so they are counted here and
    dropped later unless the admin allows them."""
    parts, non_text = [], 0
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        role = msg.get("role", "")
        if isinstance(content, str):
            parts.append(TextPart(mi, None, role, content, []))
        elif isinstance(content, list):
            for pi, item in enumerate(content):
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(TextPart(mi, pi, role, str(item.get("text", "")), []))
                else:
                    non_text += 1
    return parts, non_text


def collect_findings(parts: list[TextPart]) -> list[dict]:
    """One row per distinct value, for the review dialog and the report."""
    last_user = max((i for i, p in enumerate(parts) if p.role == "user"), default=-1)
    findings: "OrderedDict[str, dict]" = OrderedDict()
    for i, part in enumerate(parts):
        for ent in part.entities:
            key = normalize_key(ent.text)
            row = findings.get(key)
            if row is None:
                row = findings[key] = {
                    "key": key,
                    "text": ent.text,
                    "type": ent.type,
                    "sources": [],
                    "count": 0,
                    "in_latest": False,
                }
            row["count"] += 1
            if ent.source not in row["sources"]:
                row["sources"].append(ent.source)
            row["in_latest"] = row["in_latest"] or i == last_user
    return list(findings.values())


def sanitize_review_result(result, known_keys: set) -> Optional[dict]:
    """Validate what the browser sent back. Returns None when the user did not confirm."""
    if not isinstance(result, dict) or result.get("action") != "send":
        return None
    disabled = [k for k in result.get("disabled", []) if isinstance(k, str) and k in known_keys]
    added = []
    for term in result.get("added", [])[:50]:
        if isinstance(term, str):
            term = " ".join(term.split())
            if 2 <= len(term) <= 200 and normalize_key(term) not in {normalize_key(a) for a in added}:
                added.append(term)
    return {"disabled": disabled, "added": added}


# --------------------------------------------------------------------------
# Where the data goes
# --------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
        "kind": "openai",
        "operator": "OpenAI",
        "jurisdiction": "🇺🇸 United States",
    },
    "gemini": {
        "label": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "kind": "openai",
        "operator": "Google",
        "jurisdiction": "🇺🇸 United States",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-5",
        "kind": "anthropic",
        "operator": "Anthropic",
        "jurisdiction": "🇺🇸 United States",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4.1-mini",
        "kind": "openai",
        "operator": "OpenRouter, which forwards to the model's own provider",
        "jurisdiction": "🇺🇸 United States",
    },
}


def build_flow(server_location: str, apertus_location: Optional[str], cfg: dict, hidden: int) -> list[dict]:
    """The hops a message takes, what each one sees, and where it is."""
    nodes = [
        {"name": "You", "place": "Your device", "sees": "Your original text", "raw": True},
        {"name": "Open WebUI", "place": server_location, "sees": "Your original text", "raw": True},
    ]
    if apertus_location:
        nodes.append(
            {
                "name": "Apertus",
                "place": apertus_location,
                "sees": "Your original text, only to find what to hide. It sends back a list, nothing else.",
                "raw": True,
            }
        )
    nodes.append(
        {
            "name": cfg["label"],
            "place": f"{cfg['jurisdiction']} · {cfg['operator']}",
            "sees": f"Redacted text: {hidden} item(s) replaced by placeholders",
            "raw": False,
        }
    )
    return nodes


# --------------------------------------------------------------------------
# UI: review dialog (runs in the user's browser) and privacy report (chat embed)
# --------------------------------------------------------------------------

TYPE_COLORS = {
    "PERSON": "#f59e0b",
    "CONTEXTUAL": "#a855f7",
    "LOCATION": "#10b981",
    "ADDRESS": "#10b981",
    "POSTAL": "#10b981",
    "ORG": "#3b82f6",
    "CUSTOM": "#ef4444",
}
DEFAULT_TYPE_COLOR = "#64748b"

# Run through Open WebUI's `execute` event: the code runs in the chat page and its
# return value is sent back to this function. Every value from the conversation is
# inserted with textContent, never as HTML.
REVIEW_JS = r"""
const DATA = __CLAUSURUS_DATA__;
return await new Promise((resolve) => {
  let done = false, timer = null, root = null, onKey = null;
  const finish = (result) => {
    if (done) return;
    done = true;
    if (timer) clearTimeout(timer);
    if (onKey) document.removeEventListener('keydown', onKey, true);
    if (root) root.remove();
    resolve(result);
  };
  try {
    const dark = document.documentElement.classList.contains('dark');
    const C = dark
      ? { bg: '#171717', fg: '#f5f5f5', muted: '#a3a3a3', line: '#2e2e2e', card: '#222222', btn: '#2e2e2e' }
      : { bg: '#ffffff', fg: '#171717', muted: '#525252', line: '#e5e5e5', card: '#f5f5f5', btn: '#eeeeee' };
    const colorOf = (t) => DATA.colors[t] || DATA.default_color;
    const el = (tag, css, text) => {
      const n = document.createElement(tag);
      if (css) n.style.cssText = css;
      if (text !== undefined) n.textContent = text;
      return n;
    };
    const chip = (text, color) => el('span',
      `display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;` +
      `background:${color}22;color:${color};border:1px solid ${color}55;white-space:nowrap;`, text);

    const enabled = {};
    DATA.findings.forEach((f) => { enabled[f.key] = f.enabled !== false; });
    const added = [];
    let showRedacted = false;

    root = el('div', 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.55);display:flex;' +
      'align-items:center;justify-content:center;padding:16px;');
    const panel = el('div', `background:${C.bg};color:${C.fg};border-radius:16px;max-width:820px;width:100%;` +
      `max-height:92vh;overflow:auto;padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.4);font-size:14px;line-height:1.5;`);
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', 'Clausurus: review before sending');
    root.appendChild(panel);

    panel.appendChild(el('div', 'font-size:18px;font-weight:650;', '🔒 Review before sending to ' + DATA.provider));
    panel.appendChild(el('div', `color:${C.muted};margin:2px 0 16px;`,
      `Highlighted text will be replaced by placeholders before it leaves for ${DATA.provider}. ` +
      'Click a highlight or untick a row to send it as written. Add anything that was missed.'));

    const sectionTitle = (text) => el('div', `font-weight:600;margin:14px 0 6px;`, text);

    // --- preview of the latest message
    const previewHead = el('div', 'display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;');
    previewHead.appendChild(sectionTitle('Your message'));
    const modeBtn = el('button', `border:1px solid ${C.line};background:${C.btn};color:${C.fg};border-radius:8px;` +
      'padding:4px 10px;font-size:12px;cursor:pointer;');
    previewHead.appendChild(modeBtn);
    panel.appendChild(previewHead);
    const preview = el('div', `white-space:pre-wrap;word-break:break-word;background:${C.card};border-radius:12px;` +
      'padding:12px;max-height:240px;overflow:auto;');
    panel.appendChild(preview);
    if (DATA.latest_truncated) {
      panel.appendChild(el('div', `color:${C.muted};font-size:12px;margin-top:4px;`,
        'Long message: only the beginning is previewed. Everything is still checked.'));
    }

    const spansToShow = () => {
      const text = DATA.latest_text, lower = text.toLowerCase();
      const spans = DATA.latest_spans.map((s) => ({ ...s, on: !!enabled[s.key] }));
      added.forEach((term) => {
        const t = term.toLowerCase();
        let i = 0;
        while (t && (i = lower.indexOf(t, i)) !== -1) {
          const s = { start: i, end: i + t.length, key: 'user:' + t, type: 'CUSTOM', on: true, term };
          if (!spans.some((o) => o.on && s.start < o.end && o.start < s.end)) spans.push(s);
          i += t.length;
        }
      });
      spans.sort((a, b) => a.start - b.start || b.end - a.end);
      const out = [];
      let cursor = 0;
      spans.forEach((s) => { if (s.start >= cursor) { out.push(s); cursor = s.end; } });
      return out;
    };

    const renderPreview = () => {
      modeBtn.textContent = showRedacted ? 'Show my original text' : `Show what ${DATA.provider} receives`;
      preview.textContent = '';
      const text = DATA.latest_text;
      let cursor = 0;
      spansToShow().forEach((s) => {
        preview.appendChild(document.createTextNode(text.slice(cursor, s.start)));
        const original = text.slice(s.start, s.end);
        const color = colorOf(s.type);
        let node;
        if (s.on && showRedacted) {
          node = el('span', `font-family:ui-monospace,monospace;font-size:12px;padding:0 4px;border-radius:4px;` +
            `background:${color}33;color:${color};`, `[${s.type}]`);
        } else if (s.on) {
          node = el('mark', `background:${color}33;color:inherit;border-bottom:2px solid ${color};border-radius:3px;` +
            'padding:0 1px;cursor:pointer;', original);
          node.title = `${s.type}: will be hidden. Click to send as written.`;
        } else {
          node = el('span', `border-bottom:2px dashed ${color};cursor:pointer;`, original);
          node.title = `${s.type}: will be sent as written. Click to hide it.`;
        }
        node.addEventListener('click', () => {
          if (s.key.startsWith('user:')) {
            const idx = added.indexOf(s.term);
            if (idx >= 0) added.splice(idx, 1);
          } else {
            enabled[s.key] = !enabled[s.key];
          }
          renderAll();
        });
        preview.appendChild(node);
        cursor = s.end;
      });
      preview.appendChild(document.createTextNode(text.slice(cursor)));
    };
    modeBtn.addEventListener('click', () => { showRedacted = !showRedacted; renderPreview(); });

    // --- findings list
    const listTitle = sectionTitle('');
    panel.appendChild(listTitle);
    const list = el('div', `border:1px solid ${C.line};border-radius:12px;overflow:hidden;`);
    panel.appendChild(list);

    const sourceLabel = { rule: 'rules', apertus: 'Apertus', user: 'you' };
    const row = (checked, text, type, sources, note, onToggle) => {
      const r = el('label', `display:flex;align-items:center;gap:10px;padding:8px 12px;border-top:1px solid ${C.line};` +
        'cursor:pointer;flex-wrap:wrap;');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = checked;
      box.addEventListener('change', () => onToggle(box.checked));
      r.appendChild(box);
      r.appendChild(el('span', `flex:1 1 180px;min-width:0;word-break:break-word;${checked ? '' : `color:${C.muted};text-decoration:line-through;`}`, text));
      r.appendChild(chip(type, colorOf(type)));
      r.appendChild(chip('found by ' + sources.map((s) => sourceLabel[s] || s).join(' + '), C.muted));
      if (note) r.appendChild(el('span', `font-size:12px;color:${C.muted};`, note));
      return r;
    };

    const renderList = () => {
      list.textContent = '';
      const hidden = DATA.findings.filter((f) => enabled[f.key]).length + added.length;
      listTitle.textContent = `What will be hidden (${hidden} of ${DATA.findings.length + added.length})`;
      if (providerSees) providerSees.textContent = `Redacted text: ${hidden} item(s) replaced by placeholders`;
      if (!DATA.findings.length && !added.length) {
        list.appendChild(el('div', `padding:10px 12px;color:${C.muted};`, 'Nothing detected. Add anything you want hidden below.'));
      }
      DATA.findings.forEach((f) => {
        list.appendChild(row(enabled[f.key], f.text, f.type, f.sources, f.in_latest ? '' : 'earlier in this chat',
          (on) => { enabled[f.key] = on; renderAll(); }));
      });
      added.forEach((term, i) => {
        list.appendChild(row(true, term, 'CUSTOM', ['user'], 'added by you',
          (on) => { if (!on) { added.splice(i, 1); renderAll(); } }));
      });
      if (list.firstChild) list.firstChild.style.borderTop = 'none';
    };

    // --- add missed text
    panel.appendChild(sectionTitle('Missed something?'));
    const addRow = el('div', 'display:flex;gap:8px;flex-wrap:wrap;');
    const input = el('input', `flex:1 1 220px;border:1px solid ${C.line};background:${C.card};color:${C.fg};` +
      'border-radius:8px;padding:6px 10px;font-size:14px;');
    input.placeholder = 'Type a word or phrase to hide, e.g. "the baker on Main Street"';
    const addBtn = el('button', `border:1px solid ${C.line};background:${C.btn};color:${C.fg};border-radius:8px;` +
      'padding:6px 12px;cursor:pointer;', 'Hide this');
    const selBtn = el('button', `border:1px solid ${C.line};background:${C.btn};color:${C.fg};border-radius:8px;` +
      'padding:6px 12px;cursor:pointer;', 'Hide selected text');
    const addTerm = (raw) => {
      const term = (raw || '').replace(/\s+/g, ' ').trim();
      if (term.length < 2 || term.length > 200) return;
      const existing = DATA.findings.find((f) => f.key === term.toLowerCase());
      if (existing) { enabled[existing.key] = true; renderAll(); return; }
      if (added.some((a) => a.toLowerCase() === term.toLowerCase())) return;
      added.push(term);
      renderAll();
    };
    addBtn.addEventListener('click', () => { addTerm(input.value); input.value = ''; input.focus(); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); addTerm(input.value); input.value = ''; }
    });
    selBtn.addEventListener('mousedown', (e) => e.preventDefault());
    selBtn.addEventListener('click', () => addTerm(String(window.getSelection() || '')));
    addRow.appendChild(input);
    addRow.appendChild(addBtn);
    addRow.appendChild(selBtn);
    panel.appendChild(addRow);

    // --- where the data goes
    panel.appendChild(sectionTitle('Where your data goes'));
    const style = el('style');
    style.textContent = '.clz-flow{display:flex;align-items:stretch;gap:6px}' +
      '.clz-arrow{align-self:center;font-size:18px}' +
      '@media (max-width:640px){.clz-flow{flex-direction:column}.clz-arrow{transform:rotate(90deg)}' +
      '.clz-card{flex:0 0 auto !important}}';
    root.appendChild(style);
    const flow = el('div');
    flow.className = 'clz-flow';
    let providerSees = null;
    DATA.flow.forEach((n, i) => {
      if (i > 0) {
        const arrow = el('div', `color:${C.muted};`, '→');
        arrow.className = 'clz-arrow';
        flow.appendChild(arrow);
      }
      const accent = n.raw ? '#f59e0b' : '#10b981';
      const card = el('div', `flex:1 1 150px;border:1px solid ${C.line};border-top:3px solid ${accent};` +
        `border-radius:10px;padding:8px 10px;background:${C.card};`);
      card.className = 'clz-card';
      card.appendChild(el('div', 'font-weight:600;', n.name));
      card.appendChild(el('div', `font-size:12px;color:${C.muted};`, n.place));
      const sees = el('div', `font-size:12px;margin-top:4px;color:${accent};`, n.sees);
      if (!n.raw) providerSees = sees;
      card.appendChild(sees);
      flow.appendChild(card);
    });
    panel.appendChild(flow);
    panel.appendChild(el('div', `font-size:12px;color:${C.muted};margin-top:6px;`,
      'Orange: sees your original text. Green: sees only the redacted text. External providers are shown by ' +
      'company jurisdiction; they do not disclose which region serves a given request.'));

    // --- actions
    const actions = el('div', 'display:flex;justify-content:flex-end;gap:8px;margin-top:18px;flex-wrap:wrap;');
    const cancel = el('button', `border:1px solid ${C.line};background:${C.btn};color:${C.fg};border-radius:10px;` +
      'padding:8px 16px;cursor:pointer;', 'Cancel');
    const send = el('button', 'border:none;background:#e11d48;color:#fff;border-radius:10px;padding:8px 16px;' +
      'font-weight:600;cursor:pointer;', 'Send to ' + DATA.provider);
    cancel.addEventListener('click', () => finish({ action: 'cancel' }));
    send.addEventListener('click', () => finish({
      action: 'send',
      disabled: DATA.findings.filter((f) => !enabled[f.key]).map((f) => f.key),
      added: added.slice(),
    }));
    actions.appendChild(cancel);
    actions.appendChild(send);
    panel.appendChild(actions);

    const renderAll = () => { renderPreview(); renderList(); };
    renderAll();

    onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); finish({ action: 'cancel' }); } };
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(root);
    send.focus({ preventScroll: true });
    panel.scrollTop = 0;
    timer = setTimeout(() => finish({ action: 'timeout' }), DATA.timeout_ms);
  } catch (err) {
    finish({ action: 'error', message: String((err && err.message) || err) });
  }
});
"""


def build_review_code(data: dict) -> str:
    # json.dumps output is a valid JavaScript literal; "</" is escaped for good measure.
    return REVIEW_JS.replace("__CLAUSURUS_DATA__", json.dumps(data).replace("</", "<\\/"))


def build_report_html(rows: list[dict], kept: list[dict], flow: list[dict], provider: str) -> str:
    """Self-contained HTML shown under the reply (sandboxed iframe, no network access
    needed). Every value is HTML-escaped."""
    esc = html.escape

    def chip(text, color):
        return (
            f'<span class="chip" style="color:{color};background:{color}22;border-color:{color}55">'
            f"{esc(text)}</span>"
        )

    source_label = {"rule": "rules", "apertus": "Apertus", "user": "you"}
    body_rows = "".join(
        "<tr>"
        f"<td>{esc(r['text'])}</td>"
        f"<td><code>{esc(r['placeholder'])}</code></td>"
        f"<td>{chip(r['type'], TYPE_COLORS.get(r['type'], DEFAULT_TYPE_COLOR))}</td>"
        f"<td class='muted'>{esc(' + '.join(source_label.get(s, s) for s in r['sources']))}</td>"
        "</tr>"
        for r in rows[:80]
    )
    if not body_rows:
        body_rows = '<tr><td colspan="4" class="muted">Nothing was hidden.</td></tr>'
    kept_html = ""
    if kept:
        kept_html = (
            '<p class="muted">Sent as written, at your request: '
            + ", ".join(f"<b>{esc(k['text'])}</b>" for k in kept[:40])
            + "</p>"
        )
    flow_html = '<span class="arrow">→</span>'.join(
        f'<div class="node {"raw" if n["raw"] else "safe"}"><b>{esc(n["name"])}</b>'
        f'<div class="muted">{esc(n["place"])}</div><div class="sees">{esc(n["sees"])}</div></div>'
        for n in flow
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#171717;--muted:#525252;--line:#e5e5e5;--card:#f7f7f7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171717;--fg:#f5f5f5;--muted:#a3a3a3;--line:#2e2e2e;--card:#222}}}}
body{{margin:0;padding:14px 16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}}
h1{{font-size:15px;margin:0 0 2px}} .muted{{color:var(--muted);font-size:12px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;margin:8px 0}}
td,th{{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top;word-break:break-word}}
th{{font-size:12px;color:var(--muted);font-weight:600;border-top:none}}
code{{font-size:12px}} .chip{{font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;border:1px solid}}
.flow{{display:flex;flex-wrap:wrap;gap:6px;align-items:stretch;margin-top:6px}}
.node{{flex:1 1 140px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--card)}}
.node.raw{{border-top:3px solid #f59e0b}} .node.safe{{border-top:3px solid #10b981}}
.raw .sees{{color:#b45309;font-size:12px;margin-top:4px}} .safe .sees{{color:#047857;font-size:12px;margin-top:4px}}
@media (prefers-color-scheme:dark){{.raw .sees{{color:#fbbf24}} .safe .sees{{color:#34d399}}}}
.arrow{{align-self:center;color:var(--muted)}}
code,.chip{{white-space:nowrap}}
@media (max-width:560px){{
  thead{{display:none}} tr{{display:block;border-top:1px solid var(--line);padding:6px 0}}
  td{{display:inline-block;border:none;padding:2px 8px 2px 0}} td:first-child{{display:block;font-weight:600}}
  .flow{{flex-direction:column}} .node{{flex:0 0 auto}} .arrow{{transform:rotate(90deg)}}
}}
</style></head><body>
<h1>🔒 Clausurus privacy report</h1>
<div class="muted">What {esc(provider)} did not see in this request.</div>
<div class="wrap"><table><thead><tr><th>Your text</th><th>Sent as</th><th>Type</th><th>Found by</th></tr></thead>
<tbody>{body_rows}</tbody></table></div>
{kept_html}
<div style="font-weight:600;margin-top:10px">Where your data went</div>
<div class="flow">{flow_html}</div>
<p class="muted">Detection is automatic and can miss things, and remaining details may still point to a
person. External providers are shown by company jurisdiction; they do not disclose which region served
the request.</p>
<script>
function h(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
addEventListener('load',h);new ResizeObserver(h).observe(document.body);
</script></body></html>"""


# --------------------------------------------------------------------------
# Provider adapters (OpenAI-compatible + native Anthropic)
# --------------------------------------------------------------------------

PLACEHOLDER_NOTE = (
    "Some personal details in this conversation were replaced by placeholders such as [PERSON_1] "
    "or [CONTEXT_1: ...] before it was sent to you. Treat each placeholder as the thing it stands "
    "for and repeat it exactly, brackets included, when you refer to it. Do not try to guess the "
    "original values."
)


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
            choices = obj.get("choices") or [{}]
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                yield piece


async def stream_anthropic(
    client: httpx.AsyncClient, base_url: str, api_key: str, model: str, messages: list[dict]
) -> AsyncGenerator[str, None]:
    system_parts = [m["content"] for m in messages if m["role"] == "system" and isinstance(m["content"], str)]
    chat_messages = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    payload = {"model": model, "max_tokens": 4096, "stream": True, "messages": chat_messages}
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
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


class Pipe:
    class Valves(BaseModel):
        # Apertus is used only to detect what to hide, never to answer.
        apertus_api_base: str = Field(
            default="https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1",
            description="OpenAI-compatible Apertus endpoint used only to detect personal data.",
        )
        apertus_api_key: str = Field(default="", description="Key for the Apertus endpoint.")
        apertus_model: str = Field(default="swiss-ai/Apertus-v1.5-70B")
        apertus_location: str = Field(
            default="🇨🇭 Zürich, Switzerland · Swisscom",
            description="Shown to users in the data-flow view. Update it if you change the endpoint.",
        )
        apertus_timeout_seconds: float = Field(default=20.0)
        enable_apertus: bool = Field(default=True, description="Off = rules and checksums only.")
        block_on_apertus_failure: bool = Field(
            default=True,
            description="If Apertus fails: block the request (True) or send with rules-only redaction and a warning.",
        )
        server_location: str = Field(
            default="Where this Open WebUI runs (set by the admin)",
            description="Shown to users in the data-flow view, e.g. '🇨🇭 Zürich, Switzerland · AWS eu-central-2'.",
        )
        allow_images: bool = Field(
            default=False,
            description="Images and files can't be redacted. Off = they are not sent to the provider.",
        )
        review_timeout_seconds: int = Field(default=300, description="Close the review dialog (and cancel) after this.")

        # Admin fallback keys: for demos only. Leave empty in production so users bring their own.
        openai_api_key: str = Field(default="")
        gemini_api_key: str = Field(default="")
        anthropic_api_key: str = Field(default="")
        openrouter_api_key: str = Field(default="")

        openai_model: str = Field(default=PROVIDER_DEFAULTS["openai"]["default_model"])
        gemini_model: str = Field(default=PROVIDER_DEFAULTS["gemini"]["default_model"])
        anthropic_model: str = Field(default=PROVIDER_DEFAULTS["anthropic"]["default_model"])
        openrouter_model: str = Field(default=PROVIDER_DEFAULTS["openrouter"]["default_model"])

    class UserValves(BaseModel):
        review_before_sending: bool = Field(
            default=True, description="Show what will be hidden and let me change it before each message is sent."
        )
        show_privacy_report: bool = Field(default=True, description="Add a privacy report under each reply.")
        openai_api_key: str = Field(default="", description="Your own OpenAI key.")
        gemini_api_key: str = Field(default="", description="Your own Gemini key.")
        anthropic_api_key: str = Field(default="", description="Your own Anthropic key.")
        openrouter_api_key: str = Field(default="", description="Your own OpenRouter key.")

    def __init__(self):
        self.valves = self.Valves()
        # Review decisions per (user, chat): values the user chose to send as written and
        # terms they added. Kept in this server process only (see README, Limits).
        self._decisions: "OrderedDict[tuple, dict]" = OrderedDict()

    def pipes(self) -> list[dict]:
        return [{"id": key, "name": f"Clausurus 🔒 · {cfg['label']}"} for key, cfg in PROVIDER_DEFAULTS.items()]

    def _decisions_for(self, user_id: str, chat_id: Optional[str]) -> dict:
        key = (user_id, chat_id or "")
        if key not in self._decisions:
            self._decisions[key] = {"disabled": set(), "added": []}
            while len(self._decisions) > 1000:
                self._decisions.popitem(last=False)
        self._decisions.move_to_end(key)
        return self._decisions[key]

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
        __chat_id__: Optional[str] = None,
        __task__: Optional[str] = None,
    ):
        provider_key = (body.get("model") or "").split(".", 1)[-1]
        cfg = PROVIDER_DEFAULTS.get(provider_key)
        if cfg is None:
            yield f"Clausurus: unknown provider '{provider_key}'."
            return

        user = __user__ or {}
        user_valves = user.get("valves") or self.UserValves()
        interactive = not __task__  # title/tag/follow-up generation runs silently

        async def status(text: str, done: bool = True):
            if __event_emitter__ and interactive:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done, "hidden": False}})

        api_key = getattr(user_valves, f"{provider_key}_api_key", "") or getattr(self.valves, f"{provider_key}_api_key", "")
        if not api_key:
            yield (
                f"Clausurus: no API key for {cfg['label']}. Add your own key in Settings → "
                f"Functions → Clausurus (bring your own key)."
            )
            return
        model = getattr(self.valves, f"{provider_key}_model") or cfg["default_model"]

        use_apertus = self.valves.enable_apertus
        if use_apertus and not self.valves.apertus_api_key:
            if self.valves.block_on_apertus_failure:
                yield "Clausurus: Apertus is enabled but has no API key. An admin needs to set it in the function's valves."
                return
            use_apertus = False

        messages = body.get("messages", [])
        parts, non_text = split_messages(messages)
        decisions = self._decisions_for(user.get("id", ""), __chat_id__)

        await status("🔒 Clausurus: looking for personal data…", done=False)
        apertus_error = None
        async with httpx.AsyncClient() as client:
            for part in parts:
                if not part.text.strip():
                    continue
                found, error = await detect_entities(
                    part.text,
                    use_apertus,
                    client,
                    self.valves.apertus_api_base,
                    self.valves.apertus_api_key,
                    self.valves.apertus_model,
                    self.valves.apertus_timeout_seconds,
                )
                apertus_error = apertus_error or error
                part.entities = merge_entities(found + term_entities(part.text, decisions["added"]), part.text)

            if apertus_error:
                if self.valves.block_on_apertus_failure:
                    await status("🔒 Clausurus blocked this request: Apertus could not be reached.")
                    yield (
                        "Clausurus blocked this request because the Apertus detection service failed "
                        f"({apertus_error}). Nothing was sent to {cfg['label']}. An admin can allow a "
                        "rules-only fallback in the function's valves."
                    )
                    return
                await status("⚠️ Clausurus: Apertus unavailable, hiding with rules only.", done=False)

            send_as_written = set(decisions["disabled"])

            # --- review in the browser
            if interactive and __event_call__ and user_valves.review_before_sending:
                findings = collect_findings(parts)
                for f in findings:
                    f["enabled"] = f["key"] not in decisions["disabled"]
                latest = next((p for p in reversed(parts) if p.role == "user"), None)
                latest_text = latest.text if latest else ""
                limit = 6000
                data = {
                    "provider": cfg["label"],
                    "findings": findings,
                    "latest_text": latest_text[:limit],
                    "latest_truncated": len(latest_text) > limit,
                    "latest_spans": [
                        {"start": e.start, "end": e.end, "key": normalize_key(e.text), "type": e.type}
                        for e in (latest.entities if latest else [])
                        if e.end <= limit
                    ],
                    "flow": build_flow(
                        self.valves.server_location,
                        self.valves.apertus_location if use_apertus else None,
                        cfg,
                        len(findings),
                    ),
                    "colors": TYPE_COLORS,
                    "default_color": DEFAULT_TYPE_COLOR,
                    "timeout_ms": self.valves.review_timeout_seconds * 1000,
                }
                await status("🔒 Clausurus: waiting for your review…", done=False)
                try:
                    result = await asyncio.wait_for(
                        __event_call__({"type": "execute", "data": {"code": build_review_code(data)}}),
                        timeout=self.valves.review_timeout_seconds + 10,
                    )
                except asyncio.TimeoutError:
                    result = {"action": "timeout"}
                review = sanitize_review_result(result, {f["key"] for f in findings})
                if review is None:
                    reason = result.get("action") if isinstance(result, dict) else "no answer"
                    await status("🔒 Clausurus: not sent.")
                    yield (
                        f"Clausurus: nothing was sent to {cfg['label']} ({reason}). You can turn off the "
                        "review step in Settings → Functions → Clausurus."
                    )
                    return
                # The dialog showed every current finding, so its answer replaces the earlier
                # decisions for those values. A remembered added term that is unticked is
                # forgotten rather than recorded as "send as written".
                send_as_written = set(review["disabled"])
                remembered = {normalize_key(a): a for a in decisions["added"]}
                for key in send_as_written & remembered.keys():
                    decisions["added"].remove(remembered[key])
                current = {f["key"] for f in findings}
                decisions["disabled"] = (decisions["disabled"] - current) | (send_as_written - remembered.keys())
                for term in review["added"]:
                    if normalize_key(term) not in {normalize_key(a) for a in decisions["added"]}:
                        decisions["added"].append(term)
                for part in parts:
                    part.entities = merge_entities(part.entities + term_entities(part.text, review["added"]), part.text)

            sources_by_key = {f["key"]: f["sources"] for f in collect_findings(parts)}
            kept_visible: "OrderedDict[str, str]" = OrderedDict()
            for part in parts:
                for e in part.entities:
                    if normalize_key(e.text) in send_as_written:
                        kept_visible.setdefault(normalize_key(e.text), e.text)
                part.entities = [e for e in part.entities if normalize_key(e.text) not in send_as_written]

            # --- build the outgoing conversation
            anonymizer = Anonymizer()
            outgoing = [dict(m) for m in messages]
            for m in outgoing:
                if isinstance(m.get("content"), list):
                    m["content"] = [dict(item) if isinstance(item, dict) else item for item in m["content"]]
            for part in parts:
                redacted = anonymizer.anonymize(part.text, part.entities)
                if part.part_index is None:
                    outgoing[part.message_index]["content"] = redacted
                else:
                    outgoing[part.message_index]["content"][part.part_index]["text"] = redacted
            dropped = 0
            if not self.valves.allow_images:
                for m in outgoing:
                    if isinstance(m.get("content"), list):
                        kept = [i for i in m["content"] if isinstance(i, dict) and i.get("type") == "text"]
                        dropped += len(m["content"]) - len(kept)
                        m["content"] = kept
            outgoing.insert(0, {"role": "system", "content": PLACEHOLDER_NOTE})

            hidden = anonymizer.placeholder_to_value
            counts: dict[str, int] = {}
            for ptype in anonymizer.placeholder_types.values():
                counts[ptype] = counts.get(ptype, 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
            note = f"; {dropped} image/file part(s) not sent" if dropped else ""
            await status(
                f"🔒 Clausurus: {len(hidden)} item(s) hidden from {cfg['label']}"
                + (f" ({summary})" if summary else "")
                + note
            )

            if interactive and __event_emitter__ and user_valves.show_privacy_report:
                rows = [
                    {
                        "text": value,
                        "placeholder": placeholder,
                        "type": anonymizer.placeholder_types[placeholder],
                        "sources": sources_by_key.get(normalize_key(value), []),
                    }
                    for placeholder, value in hidden.items()
                ]
                kept = [{"text": t} for t in kept_visible.values()]
                flow = build_flow(
                    self.valves.server_location,
                    self.valves.apertus_location if use_apertus else None,
                    cfg,
                    len(hidden),
                )
                await __event_emitter__(
                    {"type": "embeds", "data": {"embeds": [build_report_html(rows, kept, flow, cfg["label"])]}}
                )

            # --- send and restore
            deanon = StreamDeAnonymizer(hidden)
            try:
                if cfg["kind"] == "anthropic":
                    stream = stream_anthropic(client, cfg["base_url"], api_key, model, outgoing)
                else:
                    stream = stream_openai_compatible(client, cfg["base_url"], api_key, model, outgoing)
                async for piece in stream:
                    out = deanon.feed(piece)
                    if out:
                        yield out
                tail = deanon.flush()
                if tail:
                    yield tail
            except Exception as e:  # noqa: BLE001
                yield f"\n\nClausurus: the request to {cfg['label']} failed: {e}"
