"""
pytest suite for Clausurus.

Run from this directory or repo root with:
    pytest community/owui_functions/clausurus/tests -v

Apertus and the external LLM providers are always mocked here — no network
calls, no API keys required.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

CLAUSURUS_PATH = Path(__file__).resolve().parents[1] / "clausurus.py"
spec = importlib.util.spec_from_file_location("clausurus", CLAUSURUS_PATH)
clausurus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clausurus)


# --------------------------------------------------------------------------
# Checksum validators
# --------------------------------------------------------------------------


class TestChecksums:
    def test_ahv_valid(self):
        # 756.1234.5678.97 style: 12-digit body + valid EAN-13 check digit
        body = "756123456789"
        nums = [int(d) for d in body]
        total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(nums))
        check = (10 - total % 10) % 10
        assert clausurus.validate_ahv_checksum(body + str(check)) is True

    def test_ahv_invalid_checksum(self):
        assert clausurus.validate_ahv_checksum("7561234567890") is False or True  # sanity: no crash
        # explicit wrong check digit
        good = "7561234567897"
        digits = list(good)
        digits[-1] = str((int(digits[-1]) + 1) % 10)
        assert clausurus.validate_ahv_checksum("".join(digits)) is False

    def test_ahv_wrong_length(self):
        assert clausurus.validate_ahv_checksum("756123") is False

    def test_ahv_non_digit(self):
        assert clausurus.validate_ahv_checksum("756abc4567897") is False

    def test_iban_valid_ch(self):
        # Known-valid test IBAN (widely used as an ISO example)
        assert clausurus.validate_iban_checksum("CH9300762011623852957") is True

    def test_iban_invalid(self):
        assert clausurus.validate_iban_checksum("CH9300762011623852958") is False

    def test_iban_too_short(self):
        assert clausurus.validate_iban_checksum("CH93") is False

    def test_uid_valid(self):
        # CHE-109.322.551 is a commonly cited valid example UID
        assert clausurus.validate_uid_checksum("109322551") is True

    def test_uid_invalid(self):
        assert clausurus.validate_uid_checksum("109322552") is False

    def test_luhn_valid(self):
        assert clausurus.validate_luhn("4111111111111111") is True  # well-known test Visa number

    def test_luhn_invalid(self):
        assert clausurus.validate_luhn("4111111111111112") is False


# --------------------------------------------------------------------------
# Rule-based recognizers
# --------------------------------------------------------------------------


class TestRuleRecognizers:
    def test_finds_email(self):
        ents = clausurus.rule_based_entities("Contact me at heidi.beispiel@example-mail.ch please.")
        assert any(e.type == "EMAIL" and e.text == "heidi.beispiel@example-mail.ch" for e in ents)

    def test_finds_swiss_phone(self):
        ents = clausurus.rule_based_entities("Call me at +41 79 674 35 93 today.")
        assert any(e.type == "PHONE" for e in ents)

    def test_finds_ahv_only_with_valid_checksum(self):
        body = "756123456789"
        nums = [int(d) for d in body]
        total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(nums))
        check = (10 - total % 10) % 10
        valid = f"756.{body[3:7]}.{body[7:11]}.{body[11]}{check}"
        ents = clausurus.rule_based_entities(f"AHV: {valid}")
        assert any(e.type == "AHV" for e in ents)

        # a syntactically matching but checksum-invalid AHV should NOT be flagged
        invalid = "756.0000.0000.00"
        ents2 = clausurus.rule_based_entities(f"AHV: {invalid}")
        assert not any(e.type == "AHV" for e in ents2)

    def test_finds_german_address(self):
        ents = clausurus.rule_based_entities("wohnhaft an der Seestrasse 63, 1000 Faketown")
        assert any(e.type == "ADDRESS" and "Seestrasse 63" in e.text for e in ents)

    def test_finds_french_address(self):
        ents = clausurus.rule_based_entities("domicilié Rue du Lac 18, 8999 Musterhausen")
        assert any(e.type == "ADDRESS" for e in ents)

    def test_finds_italian_address(self):
        ents = clausurus.rule_based_entities("residente in Via del Lago 170, 9999 Fantasiedorf")
        assert any(e.type == "ADDRESS" for e in ents)

    def test_finds_english_address(self):
        ents = clausurus.rule_based_entities("residing at Station Avenue 38, 9999 Fantasiedorf")
        assert any(e.type == "ADDRESS" for e in ents)

    def test_finds_person_after_honorific(self):
        ents = clausurus.rule_based_entities("Sehr geehrte Frau Heidi Beispielmann,")
        assert any(e.type == "PERSON" and "Heidi Beispielmann" in e.text for e in ents)

    def test_does_not_flag_plain_sentence(self):
        ents = clausurus.rule_based_entities("The weather today is nice and sunny in general.")
        assert ents == []

    def test_postal_deduped_inside_address(self):
        ents = clausurus.rule_based_entities("wohnhaft an der Seestrasse 63, 1000 Faketown")
        postal_only = [e for e in ents if e.type == "POSTAL"]
        assert postal_only == []  # merged into the ADDRESS span, not double-reported


# --------------------------------------------------------------------------
# Anonymizer / placeholder round-trip
# --------------------------------------------------------------------------


class TestAnonymizer:
    def test_round_trip_single_entity(self):
        text = "My name is Heidi Beispielmann and I live in Zurich."
        ent = clausurus.Entity(11, 29, "Heidi Beispielmann", "PERSON", "rule")
        anon = clausurus.Anonymizer()
        redacted = anon.anonymize(text, [ent])
        assert "Heidi Beispielmann" not in redacted
        assert "[PERSON_1]" in redacted
        restored = anon.restore(redacted)
        assert restored == text

    def test_placeholder_numbering_is_stable_for_repeated_value(self):
        text = "Heidi called. Heidi called again."
        entities = [
            clausurus.Entity(0, 5, "Heidi", "PERSON", "rule"),
            clausurus.Entity(14, 19, "Heidi", "PERSON", "rule"),
        ]
        anon = clausurus.Anonymizer()
        redacted = anon.anonymize(text, entities)
        assert redacted.count("[PERSON_1]") == 2
        assert "[PERSON_2]" not in redacted

    def test_different_values_get_different_numbers(self):
        text = "Heidi and Urs met."
        entities = [
            clausurus.Entity(0, 5, "Heidi", "PERSON", "rule"),
            clausurus.Entity(10, 13, "Urs", "PERSON", "rule"),
        ]
        anon = clausurus.Anonymizer()
        redacted = anon.anonymize(text, entities)
        assert "[PERSON_1]" in redacted and "[PERSON_2]" in redacted

    def test_overlapping_entities_merge_to_longest(self):
        text = "the mayor's wife lives here"
        entities = [
            clausurus.Entity(4, 9, "mayor", "PERSON", "apertus", 0.5),
            clausurus.Entity(0, 17, "the mayor's wife", "CONTEXTUAL", "apertus", 0.8),
        ]
        anon = clausurus.Anonymizer()
        redacted = anon.anonymize(text, entities)
        assert "mayor" not in redacted or "[PERSON" not in redacted
        assert redacted.startswith("[CONTEXT_1")

    def test_multi_turn_reanonymization_is_consistent(self):
        """Each pipe() call builds a fresh Anonymizer and re-scans the whole
        history, so the same value gets the same placeholder number across
        turns as long as message order doesn't change."""
        turn1 = "Sehr geehrte Frau Heidi Beispielmann,"
        turn2 = turn1 + " Frau Heidi Beispielmann rief erneut wegen ihres Falls an."

        def anonymize_full(text):
            ents = clausurus.rule_based_entities(text)
            anon = clausurus.Anonymizer()
            return anon.anonymize(text, ents), anon

        redacted1, _ = anonymize_full(turn1)
        redacted2, _ = anonymize_full(turn2)
        assert "[PERSON_1]" in redacted1
        assert redacted2.count("[PERSON_1]") == 2


# --------------------------------------------------------------------------
# Streaming de-anonymizer (placeholder split across chunks)
# --------------------------------------------------------------------------


class TestStreamDeAnonymizer:
    def test_restores_placeholder_in_single_chunk(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi Beispielmann"})
        out = d.feed("Hello [PERSON_1], how are you?")
        assert out == "Hello Heidi Beispielmann, how are you?"

    def test_restores_placeholder_split_across_chunks(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi Beispielmann"})
        pieces = ["Hello [PER", "SON_1], how", " are you?"]
        out = "".join(d.feed(p) for p in pieces) + d.flush()
        assert out == "Hello Heidi Beispielmann, how are you?"

    def test_passes_through_unrelated_brackets(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi"})
        out = d.feed("See [1] for details.") + d.flush()
        assert out == "See [1] for details."

    def test_flush_returns_incomplete_buffer(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi"})
        out = d.feed("trailing [PERSON")
        assert out == "trailing "
        assert d.flush() == "[PERSON"


# --------------------------------------------------------------------------
# Model-prefix gating (pipes() / pipe() provider selection)
# --------------------------------------------------------------------------


class TestModelGating:
    def test_pipes_lists_all_four_providers(self):
        f = clausurus.Filter()
        ids = {p["id"] for p in f.pipes()}
        assert ids == {"openai", "gemini", "anthropic", "openrouter"}

    @pytest.mark.asyncio
    async def test_unknown_provider_id_is_rejected(self):
        f = clausurus.Filter()
        body = {"model": "clausurus.not_a_real_provider", "messages": []}
        chunks = [c async for c in f.pipe(body)]
        assert any("unknown provider" in c.lower() for c in chunks)

    @pytest.mark.asyncio
    async def test_missing_api_key_is_reported_and_nothing_is_sent(self, monkeypatch):
        f = clausurus.Filter()
        f.valves.openai_api_key = ""
        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "hi"}]}
        chunks = [c async for c in f.pipe(body, __user__={"valves": None})]
        assert any("no api key" in c.lower() for c in chunks)


# --------------------------------------------------------------------------
# Apertus-failure fallback (mocked API)
# --------------------------------------------------------------------------


class TestApertusFallback:
    @pytest.mark.asyncio
    async def test_apertus_down_blocks_by_default(self, monkeypatch):
        f = clausurus.Filter()
        f.valves.openai_api_key = "sk-test"
        f.valves.enable_apertus = True
        f.valves.block_on_apertus_failure = True

        async def boom(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(clausurus, "apertus_entities", boom)

        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Heidi lives here"}]}
        chunks = [c async for c in f.pipe(body, __user__={"valves": None})]
        joined = "".join(chunks)
        assert "blocked" in joined.lower()

    @pytest.mark.asyncio
    async def test_apertus_down_falls_back_to_rules_when_configured(self, monkeypatch):
        f = clausurus.Filter()
        f.valves.openai_api_key = "sk-test"
        f.valves.enable_apertus = True
        f.valves.block_on_apertus_failure = False

        async def boom(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(clausurus, "apertus_entities", boom)

        events = []

        async def emitter(event):
            events.append(event)

        async def fake_stream(*args, **kwargs):
            yield "ok"

        monkeypatch.setattr(clausurus, "stream_openai_compatible", fake_stream)

        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "hello there"}]}
        chunks = [c async for c in f.pipe(body, __user__={"valves": None}, __event_emitter__=emitter)]
        assert "".join(chunks) == "ok"
        assert any("apertus unavailable" in json.dumps(e).lower() for e in events)

    @pytest.mark.asyncio
    async def test_detect_entities_reports_error_without_raising(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        import unittest.mock as mock

        with mock.patch.object(clausurus, "apertus_entities", boom):
            entities, err = await clausurus.detect_entities(
                "Heidi lives at Seestrasse 63", True, None, "http://x", "key", "model", 5
            )
        assert err is not None
        # rule-based entities should still be returned even though Apertus failed
        assert any(e.type == "ADDRESS" for e in entities)


# --------------------------------------------------------------------------
# End-to-end pipe() happy path with a mocked provider
# --------------------------------------------------------------------------


class TestPipeHappyPath:
    @pytest.mark.asyncio
    async def test_redacts_before_sending_and_restores_in_output(self, monkeypatch):
        f = clausurus.Filter()
        f.valves.openai_api_key = "sk-test"
        f.valves.enable_apertus = False  # rules only, deterministic

        captured = {}

        async def fake_stream(client, base_url, api_key, model, messages):
            captured["messages"] = messages
            yield "Dear [PERSON_1], "
            yield "your request was received."

        monkeypatch.setattr(clausurus, "stream_openai_compatible", fake_stream)

        body = {
            "model": "clausurus.openai",
            "messages": [{"role": "user", "content": "Sehr geehrte Frau Heidi Beispielmann,"}],
        }
        chunks = [c async for c in f.pipe(body, __user__={"valves": None})]
        output = "".join(chunks)

        # the provider must never see the real name
        assert "Heidi Beispielmann" not in captured["messages"][0]["content"]
        assert "[PERSON_1]" in captured["messages"][0]["content"]
        # but the user-facing output has it restored
        assert "Heidi Beispielmann" in output
        assert "[PERSON_1]" not in output

    @pytest.mark.asyncio
    async def test_user_byok_key_takes_priority_over_admin_key(self, monkeypatch):
        f = clausurus.Filter()
        f.valves.openai_api_key = "admin-key"
        f.valves.enable_apertus = False

        captured = {}

        async def fake_stream(client, base_url, api_key, model, messages):
            captured["api_key"] = api_key
            yield "ok"

        monkeypatch.setattr(clausurus, "stream_openai_compatible", fake_stream)

        user_valves = clausurus.Filter.UserValves(openai_api_key="user-own-key")
        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "hi"}]}
        _ = [c async for c in f.pipe(body, __user__={"valves": user_valves})]
        assert captured["api_key"] == "user-own-key"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
