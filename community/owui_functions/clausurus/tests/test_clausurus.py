"""
pytest suite for Clausurus.

    pytest community/owui_functions/clausurus/tests -v

Apertus and the external providers are always mocked: no network, no keys.
"""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

CLAUSURUS_PATH = Path(__file__).resolve().parents[1] / "clausurus.py"
spec = importlib.util.spec_from_file_location("clausurus", CLAUSURUS_PATH)
clausurus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clausurus)


def valid_ahv(body: str = "756123456789") -> str:
    nums = [int(d) for d in body]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(nums))
    return body + str((10 - total % 10) % 10)


def make_pipe(**valves) -> "clausurus.Pipe":
    p = clausurus.Pipe()
    p.valves.openai_api_key = "sk-test"
    p.valves.enable_apertus = False
    for k, v in valves.items():
        setattr(p.valves, k, v)
    return p


def user(review=False, report=False, **keys):
    return {"id": "u1", "valves": clausurus.Pipe.UserValves(review_before_sending=review, show_privacy_report=report, **keys)}


class FakeProvider:
    """Stands in for stream_openai_compatible and records what would be sent."""

    def __init__(self, reply=("ok",)):
        self.reply = reply
        self.calls = []

    async def __call__(self, client, base_url, api_key, model, messages):
        self.calls.append({"api_key": api_key, "model": model, "messages": messages})
        for piece in self.reply:
            yield piece

    @property
    def sent_text(self) -> str:
        return json.dumps(self.calls[-1]["messages"], ensure_ascii=False)


async def run(pipe, body, **kwargs) -> str:
    return "".join([c async for c in pipe.pipe(body, **kwargs)])


# --------------------------------------------------------------------------
# Open WebUI integration contract
# --------------------------------------------------------------------------


class TestOpenWebUIContract:
    def test_class_is_named_pipe(self):
        # Open WebUI decides the function type from the class name (utils/plugin.py):
        # a class called Filter would be installed as a filter and never appear as a model.
        assert hasattr(clausurus, "Pipe")
        assert not hasattr(clausurus, "Filter")

    def test_pipes_lists_all_four_providers(self):
        ids = {p["id"] for p in clausurus.Pipe().pipes()}
        assert ids == {"openai", "gemini", "anthropic", "openrouter"}

    def test_frontmatter_description_is_single_line(self):
        header = CLAUSURUS_PATH.read_text().split('"""')[1]
        description = [line for line in header.splitlines() if line.startswith("description:")]
        assert description and len(description[0]) > len("description: ") + 10


# --------------------------------------------------------------------------
# Checksum validators
# --------------------------------------------------------------------------


class TestChecksums:
    def test_ahv_valid(self):
        assert clausurus.validate_ahv_checksum(valid_ahv()) is True

    def test_ahv_wrong_check_digit(self):
        good = valid_ahv()
        bad = good[:-1] + str((int(good[-1]) + 1) % 10)
        assert clausurus.validate_ahv_checksum(bad) is False

    def test_ahv_wrong_length_or_letters(self):
        assert clausurus.validate_ahv_checksum("756123") is False
        assert clausurus.validate_ahv_checksum("756abc4567897") is False

    def test_iban(self):
        assert clausurus.validate_iban_checksum("CH9300762011623852957") is True
        assert clausurus.validate_iban_checksum("CH9300762011623852958") is False
        assert clausurus.validate_iban_checksum("CH93") is False

    def test_uid(self):
        assert clausurus.validate_uid_checksum("109322551") is True
        assert clausurus.validate_uid_checksum("109322552") is False

    def test_luhn(self):
        assert clausurus.validate_luhn("4111111111111111") is True
        assert clausurus.validate_luhn("4111111111111112") is False


# --------------------------------------------------------------------------
# Rule-based recognizers
# --------------------------------------------------------------------------


def types_found(text):
    return {(e.type, e.text) for e in clausurus.rule_based_entities(text)}


class TestRuleRecognizers:
    def test_email_and_phone(self):
        found = types_found("Mail heidi.beispiel@example-mail.ch or call +41 79 674 35 93.")
        assert ("EMAIL", "heidi.beispiel@example-mail.ch") in found
        assert ("PHONE", "+41 79 674 35 93") in found

    def test_ahv_only_with_valid_checksum(self):
        v = valid_ahv()
        formatted = f"{v[:3]}.{v[3:7]}.{v[7:11]}.{v[11:]}"
        assert ("AHV", formatted) in types_found(f"AHV: {formatted}")
        assert not any(t == "AHV" for t, _ in types_found("AHV: 756.0000.0000.00"))

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("wohnhaft an der Seestrasse 63, 1000 Faketown", "Seestrasse 63, 1000 Faketown"),
            ("domicilié Rue du Lac 18, 8999 Musterhausen", "Rue du Lac 18, 8999 Musterhausen"),
            ("residente in Via del Lago 170, 9999 Fantasiedorf", "Via del Lago 170, 9999 Fantasiedorf"),
            ("residing at Station Avenue 38, 9999 Fantasiedorf", "Station Avenue 38, 9999 Fantasiedorf"),
        ],
    )
    def test_addresses_in_four_languages(self, text, expected):
        assert ("ADDRESS", expected) in types_found(text)

    def test_address_town_does_not_swallow_the_sentence(self):
        found = types_found("Ich wohne an der Seestrasse 63, 8000 Zürich ist schön.")
        assert ("ADDRESS", "Seestrasse 63, 8000 Zürich") in found

    def test_postcode_does_not_cross_line_breaks(self):
        assert not any(t == "POSTAL" for t, _ in types_found("24. September 2026\n\nBeschwerde betreffend"))

    def test_person_after_honorific(self):
        assert ("PERSON", "Heidi Beispielmann") in types_found("Sehr geehrte Frau Heidi Beispielmann,")

    def test_plain_sentence_has_no_findings(self):
        assert clausurus.rule_based_entities("The weather today is nice and sunny in general.") == []

    def test_postcode_inside_address_is_not_double_counted(self):
        assert not any(t == "POSTAL" for t, _ in types_found("an der Seestrasse 63, 1000 Faketown"))

    def test_user_terms_are_case_insensitive(self):
        ents = clausurus.term_entities("The Baker on Main Street said hi to the baker.", ["the baker"])
        assert [e.text for e in ents] == ["The Baker", "the baker"]
        assert all(e.type == "CUSTOM" and e.source == "user" for e in ents)


# --------------------------------------------------------------------------
# Merging and placeholders
# --------------------------------------------------------------------------


class TestMergeAndPlaceholders:
    def test_overlapping_spans_are_redacted_as_their_union(self):
        # Before the fix, the longer Apertus span replaced the rule span and
        # "Seestrasse 63, " was sent in clear.
        text = "Seestrasse 63, 1000 Faketown, Switzerland"
        rule = clausurus.Entity(0, 28, "Seestrasse 63, 1000 Faketown", "ADDRESS", "rule")
        apertus = clausurus.Entity(15, 41, "1000 Faketown, Switzerland", "LOCATION", "apertus")
        redacted = clausurus.Anonymizer().anonymize(text, [rule, apertus])
        assert redacted == "[ADDRESS_1]"

    def test_merged_span_keeps_the_rule_type(self):
        text = "Frau Heidi Beispielmann"
        merged = clausurus.merge_entities(
            [
                clausurus.Entity(5, 23, "Heidi Beispielmann", "PERSON", "apertus"),
                clausurus.Entity(0, 23, text, "PERSON", "rule"),
            ],
            text,
        )
        assert len(merged) == 1 and merged[0].source == "rule" and merged[0].text == text

    def test_round_trip(self):
        text = "My name is Heidi Beispielmann and I live in Zurich."
        anon = clausurus.Anonymizer()
        redacted = anon.anonymize(text, [clausurus.Entity(11, 29, "Heidi Beispielmann", "PERSON", "rule")])
        assert "Heidi" not in redacted and "[PERSON_1]" in redacted
        assert anon.restore(redacted) == text

    def test_same_value_same_placeholder_different_values_different(self):
        text = "Heidi called. Urs called. Heidi again."
        ents = [
            clausurus.Entity(0, 5, "Heidi", "PERSON", "rule"),
            clausurus.Entity(14, 17, "Urs", "PERSON", "rule"),
            clausurus.Entity(26, 31, "Heidi", "PERSON", "rule"),
        ]
        redacted = clausurus.Anonymizer().anonymize(text, ents)
        assert redacted == "[PERSON_1] called. [PERSON_2] called. [PERSON_1] again."

    def test_contextual_placeholder_carries_a_hint(self):
        anon = clausurus.Anonymizer()
        out = anon.anonymize("the mayor's wife", [clausurus.Entity(0, 16, "the mayor's wife", "CONTEXTUAL", "apertus")])
        assert out.startswith("[CONTEXT_1:") and anon.placeholder_types[out] == "CONTEXTUAL"


class TestStreamDeAnonymizer:
    def test_split_placeholder(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi Beispielmann"})
        out = "".join(d.feed(p) for p in ["Hello [PER", "SON_1], how", " are you?"]) + d.flush()
        assert out == "Hello Heidi Beispielmann, how are you?"

    def test_unrelated_brackets_pass_through(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi"})
        assert d.feed("See [1] and [link](http://x).") + d.flush() == "See [1] and [link](http://x)."

    def test_flush_returns_incomplete_buffer(self):
        d = clausurus.StreamDeAnonymizer({"[PERSON_1]": "Heidi"})
        assert d.feed("trailing [PERSON") == "trailing "
        assert d.flush() == "[PERSON"


# --------------------------------------------------------------------------
# Apertus (mocked HTTP)
# --------------------------------------------------------------------------


def apertus_transport(entities, counter=None, status=200):
    def handler(request):
        if counter is not None:
            counter.append(json.loads(request.content)["messages"][1]["content"])
        if status != 200:
            return httpx.Response(status)
        content = json.dumps({"entities": entities})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


class TestApertus:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        clausurus._APERTUS_CACHE.clear()

    @pytest.mark.asyncio
    async def test_only_verbatim_spans_are_kept(self):
        text = "Die einzige Apothekerin im Dorf hat angerufen."
        client = httpx.AsyncClient(
            transport=apertus_transport(
                [
                    {"text": "die einzige Apothekerin im Dorf", "type": "CONTEXTUAL"},  # wrong case: not verbatim
                    {"text": "Die einzige Apothekerin im Dorf", "type": "CONTEXTUAL"},
                    {"text": "Hans Invented", "type": "PERSON"},  # not in the text
                    {"text": "im", "type": "LOCATION"},  # too short
                ]
            )
        )
        ents = await clausurus.apertus_entities(client, "http://apertus", "k", "m", text, 5)
        assert [(e.text, e.type) for e in ents] == [("Die einzige Apothekerin im Dorf", "CONTEXTUAL")]

    @pytest.mark.asyncio
    async def test_honorific_is_kept_outside_person_spans(self):
        text = "Ich bin Frau Heidi Beispielmann."
        client = httpx.AsyncClient(transport=apertus_transport([{"text": "Frau Heidi Beispielmann", "type": "PERSON"}]))
        ents = await clausurus.apertus_entities(client, "http://apertus", "k", "m", text, 5)
        assert [e.text for e in ents] == ["Heidi Beispielmann"]
        assert clausurus.Anonymizer().anonymize(text, ents) == "Ich bin Frau [PERSON_1]."

    @pytest.mark.asyncio
    async def test_results_are_cached_per_text(self):
        calls = []
        client = httpx.AsyncClient(transport=apertus_transport([{"text": "Heidi", "type": "PERSON"}], calls))
        for _ in range(3):
            await clausurus.apertus_entities(client, "http://apertus", "k", "m", "Heidi here", 5)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_failure_is_reported_and_rules_still_apply(self, monkeypatch):
        async def no_sleep(_):
            return None

        monkeypatch.setattr(clausurus.asyncio, "sleep", no_sleep)
        calls = []
        client = httpx.AsyncClient(transport=apertus_transport([], counter=calls, status=500))
        ents, err = await clausurus.detect_entities("an der Seestrasse 63", True, client, "http://a", "k", "m", 5)
        assert err is not None
        assert any(e.type == "ADDRESS" for e in ents)
        assert len(calls) == 4  # first try + 3 retries on 5xx


# --------------------------------------------------------------------------
# pipe(): provider gating, keys, failure policy
# --------------------------------------------------------------------------


class TestPipeGatingAndFailures:
    @pytest.mark.asyncio
    async def test_unknown_provider_is_rejected(self):
        out = await run(make_pipe(), {"model": "clausurus.nope", "messages": []})
        assert "unknown provider" in out.lower()

    @pytest.mark.asyncio
    async def test_missing_key_sends_nothing(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        out = await run(make_pipe(openai_api_key=""), {"model": "clausurus.openai", "messages": [{"role": "user", "content": "hi"}]}, __user__=user())
        assert "no api key" in out.lower() and provider.calls == []

    @pytest.mark.asyncio
    async def test_user_key_beats_admin_key(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "hi"}]}
        await run(make_pipe(openai_api_key="admin"), body, __user__=user(openai_api_key="mine"))
        assert provider.calls[0]["api_key"] == "mine"

    @pytest.mark.asyncio
    async def test_apertus_failure_blocks_by_default(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)

        async def boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(clausurus, "apertus_entities", boom)
        pipe = make_pipe(enable_apertus=True, apertus_api_key="k")
        out = await run(pipe, {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Heidi"}]}, __user__=user())
        assert "blocked" in out.lower() and provider.calls == []

    @pytest.mark.asyncio
    async def test_apertus_failure_can_fall_back_to_rules(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)

        async def boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(clausurus, "apertus_entities", boom)
        events = []

        async def emitter(e):
            events.append(e)

        pipe = make_pipe(enable_apertus=True, apertus_api_key="k", block_on_apertus_failure=False)
        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Frau Heidi Muster"}]}
        out = await run(pipe, body, __user__=user(), __event_emitter__=emitter)
        assert out == "ok"
        assert "Heidi" not in provider.sent_text
        assert any("apertus unavailable" in json.dumps(e).lower() for e in events)

    @pytest.mark.asyncio
    async def test_missing_apertus_key_blocks(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        pipe = make_pipe(enable_apertus=True, apertus_api_key="")
        out = await run(pipe, {"model": "clausurus.openai", "messages": [{"role": "user", "content": "x"}]}, __user__=user())
        assert "no api key" in out.lower() and provider.calls == []


# --------------------------------------------------------------------------
# pipe(): what the provider receives
# --------------------------------------------------------------------------


class TestPipeRedaction:
    @pytest.mark.asyncio
    async def test_redacts_before_sending_and_restores_reply(self, monkeypatch):
        provider = FakeProvider(reply=("Dear [PER", "SON_1], your request was received."))
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Sehr geehrte Frau Heidi Beispielmann,"}]}
        out = await run(make_pipe(), body, __user__=user())
        sent = provider.calls[0]["messages"]
        assert sent[0]["role"] == "system" and "placeholder" in sent[0]["content"]
        assert "Heidi" not in provider.sent_text
        assert "Frau [PERSON_1]" in sent[-1]["content"]  # honorific stays, so no "Frau Frau" on restore
        assert out == "Dear Heidi Beispielmann, your request was received."

    @pytest.mark.asyncio
    async def test_multi_turn_history_is_redacted_consistently(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        body = {
            "model": "clausurus.openai",
            "messages": [
                {"role": "user", "content": "Ich bin Frau Heidi Beispielmann."},
                {"role": "assistant", "content": "Guten Tag Frau Heidi Beispielmann!"},
                {"role": "user", "content": "Meine AHV ist 756.1234.5678.97."},
            ],
        }
        await run(make_pipe(), body, __user__=user())
        sent = provider.calls[0]["messages"]
        assert "Heidi" not in provider.sent_text and "756.1234" not in provider.sent_text
        assert "[PERSON_1]" in sent[1]["content"] and "[PERSON_1]" in sent[2]["content"]

    @pytest.mark.asyncio
    async def test_multimodal_text_is_redacted_and_images_dropped(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        body = {
            "model": "clausurus.openai",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Frau Heidi Beispielmann sends a photo"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        }
        await run(make_pipe(), body, __user__=user())
        content = provider.calls[0]["messages"][-1]["content"]
        assert content == [{"type": "text", "text": "Frau [PERSON_1] sends a photo"}]
        # the caller's body must not be mutated
        assert body["messages"][0]["content"][0]["text"].startswith("Frau Heidi")

    @pytest.mark.asyncio
    async def test_task_requests_skip_review_and_report(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        called = []

        async def event_call(e):
            called.append(e)
            return {"action": "cancel"}

        async def emitter(e):
            called.append(e)

        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Title for: Frau Heidi Muster"}]}
        await run(make_pipe(), body, __user__=user(review=True, report=True), __event_call__=event_call,
                  __event_emitter__=emitter, __task__="title_generation")
        assert called == [] and len(provider.calls) == 1 and "Heidi" not in provider.sent_text


# --------------------------------------------------------------------------
# Review dialog and privacy report
# --------------------------------------------------------------------------


class TestReview:
    BODY = {
        "model": "clausurus.openai",
        "messages": [{"role": "user", "content": "Frau Heidi Beispielmann, Tel. +41 79 123 45 67, the baker"}],
    }

    async def run_with_answer(self, monkeypatch, answer, pipe=None, chat_id="c1"):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        seen = []

        async def event_call(event):
            seen.append(event)
            return answer

        pipe = pipe or make_pipe()
        out = await run(pipe, self.BODY, __user__=user(review=True), __event_call__=event_call, __chat_id__=chat_id)
        return out, provider, seen, pipe

    @pytest.mark.asyncio
    async def test_dialog_receives_findings_and_flow(self, monkeypatch):
        _, _, seen, _ = await self.run_with_answer(monkeypatch, {"action": "send", "disabled": [], "added": []})
        assert seen[0]["type"] == "execute"
        code = seen[0]["data"]["code"]
        data = json.loads(code.split("const DATA = ", 1)[1].split(";\nreturn", 1)[0])
        assert {f["key"] for f in data["findings"]} == {"heidi beispielmann", "+41 79 123 45 67"}
        assert data["flow"][-1]["name"] == "OpenAI" and data["flow"][-1]["raw"] is False
        assert "United States" in data["flow"][-1]["place"]

    @pytest.mark.asyncio
    async def test_cancel_sends_nothing(self, monkeypatch):
        out, provider, _, _ = await self.run_with_answer(monkeypatch, {"action": "cancel"})
        assert provider.calls == [] and "nothing was sent" in out.lower()

    @pytest.mark.asyncio
    async def test_browser_error_or_disconnect_sends_nothing(self, monkeypatch):
        for answer in ({"error": "Client session disconnected."}, None, True, {"action": "timeout"}):
            _, provider, _, _ = await self.run_with_answer(monkeypatch, answer)
            assert provider.calls == []

    @pytest.mark.asyncio
    async def test_user_can_unhide_and_add(self, monkeypatch):
        answer = {"action": "send", "disabled": ["+41 79 123 45 67"], "added": ["the baker"]}
        _, provider, _, _ = await self.run_with_answer(monkeypatch, answer)
        sent = provider.calls[0]["messages"][-1]["content"]
        assert "+41 79 123 45 67" in sent  # kept visible on request
        assert "the baker" not in sent and "[CUSTOM_1]" in sent  # hidden on request
        assert "Heidi" not in sent

    @pytest.mark.asyncio
    async def test_unknown_keys_from_the_browser_are_ignored(self, monkeypatch):
        answer = {"action": "send", "disabled": ["not a finding", 42], "added": ["x", 7, "a" * 500]}
        _, provider, _, _ = await self.run_with_answer(monkeypatch, answer)
        assert "Heidi" not in provider.sent_text and "+41 79" not in provider.sent_text

    @pytest.mark.asyncio
    async def test_decisions_carry_over_to_the_next_turn_and_can_be_reversed(self, monkeypatch):
        answer = {"action": "send", "disabled": ["+41 79 123 45 67"], "added": ["the baker"]}
        _, _, _, pipe = await self.run_with_answer(monkeypatch, answer)

        # next turn: the dialog shows the phone unticked and the added term ticked
        _, provider, seen, _ = await self.run_with_answer(monkeypatch, {"action": "send", "disabled": [], "added": []}, pipe=pipe)
        code = seen[0]["data"]["code"]
        data = json.loads(code.split("const DATA = ", 1)[1].split(";\nreturn", 1)[0])
        by_key = {f["key"]: f for f in data["findings"]}
        assert by_key["+41 79 123 45 67"]["enabled"] is False
        assert by_key["the baker"]["enabled"] is True and by_key["the baker"]["sources"] == ["user"]
        # ticking the phone again hides it again
        assert "+41 79" not in provider.sent_text and "the baker" not in provider.sent_text

    @pytest.mark.asyncio
    async def test_decisions_apply_without_review_too(self, monkeypatch):
        answer = {"action": "send", "disabled": [], "added": ["the baker"]}
        _, _, _, pipe = await self.run_with_answer(monkeypatch, answer)
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        await run(pipe, self.BODY, __user__=user(review=False), __chat_id__="c1")
        assert "the baker" not in provider.sent_text

    def test_review_code_escapes_script_breakers(self):
        code = clausurus.build_review_code({"latest_text": "</script><img src=x onerror=alert(1)>"})
        assert "</script>" not in code


class TestReport:
    @pytest.mark.asyncio
    async def test_report_embed_is_emitted_and_escaped(self, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(clausurus, "stream_openai_compatible", provider)
        events = []

        async def emitter(e):
            events.append(e)

        body = {"model": "clausurus.openai", "messages": [{"role": "user", "content": "Frau Heidi <b>Muster</b>"}]}
        await run(make_pipe(server_location="🇨🇭 Zürich"), body, __user__=user(report=True), __event_emitter__=emitter)
        embeds = [e for e in events if e["type"] == "embeds"]
        assert len(embeds) == 1
        page = embeds[0]["data"]["embeds"][0]
        assert "<td>Heidi</td>" in page and "[PERSON_1]" in page
        assert "<b>Muster</b>" not in page
        assert "🇨🇭 Zürich" in page and "United States" in page
        assert "iframe:height" in page

    def test_report_html_escapes_values(self):
        page = clausurus.build_report_html(
            [{"text": "<script>x</script>", "placeholder": "[PERSON_1]", "type": "PERSON", "sources": ["rule"]}],
            [{"text": "<img>"}],
            clausurus.build_flow("srv", "🇨🇭 Zürich, Switzerland · Swisscom", clausurus.PROVIDER_DEFAULTS["openai"], 1),
            "OpenAI",
        )
        assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
        assert "<img>" not in page

    def test_flow_marks_who_sees_raw_text(self):
        flow = clausurus.build_flow("srv", "🇨🇭 Zürich", clausurus.PROVIDER_DEFAULTS["gemini"], 3)
        assert [n["raw"] for n in flow] == [True, True, True, False]
        assert "3 item(s)" in flow[-1]["sees"]
        assert len(clausurus.build_flow("srv", None, clausurus.PROVIDER_DEFAULTS["gemini"], 0)) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
