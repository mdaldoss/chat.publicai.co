# Clausurus 🔒

An Open WebUI **Pipe** function that lets you send a message to an external,
bring-your-own-key LLM provider (OpenAI, Gemini, Anthropic, or OpenRouter)
with direct personal identifiers removed first. Clausurus adds four entries
to the model picker — *Clausurus 🔒 · OpenAI*, *· Gemini*, *· Anthropic*,
*· OpenRouter* — each requiring the user's own API key.

Built for the Swiss {ai} Weeks "Public AI" hackathon challenge.

## What it does

1. Reads every message in the conversation (all turns, every time — not
   just the new one, so edits and follow-ups are covered too).
2. Finds personal identifiers with two layers:
   - **Rules**: regex + checksum validation for AHV/AVS numbers, IBANs,
     Swiss UID/CHE numbers, payment cards (Luhn), emails, Swiss phone
     numbers, IP addresses, vehicle plates, and DE/FR/IT/EN-style street
     addresses and postcodes.
   - **Apertus**: an LLM call (via any OpenAI-compatible Apertus endpoint,
     configurable — the Swiss AI Weeks hackathon endpoint by default) that
     finds names, organisations, locations, and *contextual* identifiers
     rules can't — e.g. "the only pharmacist in the village" or "the
     mayor's wife". Apertus is used only for this detection step; it is
     never the provider your message is sent to unless you separately
     choose Apertus as your normal chat model in Open WebUI (Clausurus
     doesn't touch that path).
3. Replaces each finding with a numbered placeholder (`[PERSON_1]`,
   `[ADDRESS_1]`, `[CONTEXT_1: a specific local individual]`, ...),
   consistently across the conversation.
4. Sends the redacted messages to the provider and model you picked, using
   your own API key.
5. Restores the real values in the streamed reply before you see it
   (buffered so a placeholder split across two stream chunks is still
   caught).
6. Shows a status line with how many identifiers of each type were
   redacted.

If Apertus is unreachable, Clausurus **blocks the request by default**
rather than silently sending less-redacted text (configurable via valves —
see below).

## Threat model — read this before you rely on it

**What Clausurus does**: prevents the chosen external provider from seeing
the identifiers it recognizes — structured PII (AHV, IBAN, email, phone,
address, ...) and, when Apertus is enabled, personal names and some
contextual descriptions.

**What Clausurus does NOT do**:

- **It does not guarantee anonymity.** Contextual re-identification stays
  possible: if the redacted text still contains enough unusual detail (a
  rare combination of facts, a very specific date and place), a determined
  reader — human or model — may be able to work out who it's about even
  with every name removed.
- **Pseudonymized text can still be personal data.** Under the Swiss FADP
  and the GDPR, text that has had identifiers replaced with placeholders is
  not automatically "anonymous" in the legal sense if the original mapping
  exists anywhere (which it does here, for the duration of the request, in
  order to restore the reply) or if the person is still identifiable from
  context. Clausurus reduces exposure; it does not change the legal
  classification of the data on its own.
- **This is not a compliance certification.** Nobody has audited this
  against FADP, GDPR, or any other standard. Treat it as a best-effort
  technical control, not a legal opinion.
- **It only sees text.** Images, PDFs, and other attachments are blocked by
  default rather than passed through unredacted (see Limits).
- **Detection is not perfect.** See Eval results below for measured
  precision/recall and, importantly, the *leak rate* on a held-out
  synthetic test set — the number of identifiers that would have reached
  the provider unredacted.
- **Apertus itself sees the raw text.** The entity-detection call sends
  your full, unredacted message to whichever Apertus endpoint is
  configured, in order to find what to redact. By default this is the
  Swiss AI Weeks hackathon endpoint (Swisscom-hosted); point the
  `apertus_api_base` valve at a different Apertus deployment if you don't
  want that.

In short: Clausurus raises the bar for what a third-party LLM provider sees.
It is not a guarantee, and it does not replace judgment about what you
paste into a chat box.

## Valves (admin settings)

| Valve | Default | Purpose |
|---|---|---|
| `apertus_api_base` | Swiss AI Weeks hackathon endpoint | OpenAI-compatible Apertus endpoint used only for entity detection |
| `apertus_api_key` | *(empty)* | Key for the endpoint above |
| `apertus_model` | `swiss-ai/Apertus-v1.5-70B` | Model name at that endpoint |
| `enable_apertus` | `true` | Use Apertus at all (off = regex/checksums only) |
| `block_on_apertus_failure` | `true` | If Apertus is unreachable: block the request (safer) vs. send with regex-only redaction and a warning |
| `require_confirmation` | `false` | Ask the user to confirm the redacted text via a dialog before sending |
| `openai_api_key` / `gemini_api_key` / `anthropic_api_key` / `openrouter_api_key` | *(empty)* | Admin-provided fallback keys, only meant for a demo — leave empty in production so each user brings their own |
| `openai_model` / `gemini_model` / `anthropic_model` / `openrouter_model` | e.g. `gpt-4o-mini` | Model to call at each provider |

## User settings (bring your own key)

Each user sets their own key per provider in the function's per-user
valves (`openai_api_key`, `gemini_api_key`, `anthropic_api_key`,
`openrouter_api_key`). A user key always takes priority over an admin
fallback key.

## Limits

- Images and other non-text attachments are not anonymized and are
  currently sent through untouched by Clausurus if present — treat
  Clausurus as text-only and avoid attaching files/images on a Clausurus
  model until this is addressed.
- The rule-based recognizers are tuned for Swiss/DE-FR-IT-EN formats; other
  countries' ID and phone formats will mostly be missed by rules (Apertus
  may still catch some as generic PERSON/LOCATION entities).
- Apertus is called once per non-empty message per turn; on a long
  conversation this means re-scanning earlier turns every time, which adds
  latency and Apertus API usage. There is no persistent cache across
  requests (by design, to stay stateless across replicas).
- Placeholder restoration is text-based (`str.replace`); if the model's
  reply happens to contain a string identical to a placeholder token that
  wasn't meant as one, it will still be replaced.

## Eval results

See [`eval/results.md`](eval/results.md) for the full table. Summary, on 40
synthetic Swiss texts (DE/FR/IT/EN; citizen complaints, commune
correspondence, HR emails, bank-style letters; 172 gold-annotated
identifier spans; dataset generated by `eval/gen_dataset.py`, seeded and
reproducible):

- **Rules only**: perfect precision and recall (1.00 / 1.00) on structured
  identifiers it's built for (AHV, IBAN, email, phone, address), but only
  19% recall on personal names (bare names without an honorific are
  invisible to regex) and **0%** on contextual identifiers, by design.
  **63 of 172 identifiers (37%) would leak** to the external provider.
- **Rules + Apertus**: PERSON recall goes to 100% (0.74 precision — Apertus
  over-flags some names), CONTEXTUAL recall goes to 79% (0.66 precision).
  **Only 1 of 172 identifiers (0.6%) leaked** — a French contextual phrase
  ("mon voisin du troisième étage" / "my neighbour on the third floor")
  that Apertus missed. Apertus also flagged LOCATION and ORG spans (38 and
  5 respectively) that don't correspond to any gold entity in this
  dataset — over-redaction, not a privacy risk, but worth knowing before
  you rely on the output text remaining fully readable.
  Measured average latency per document was ~4.4s in this run, but that
  includes retry/backoff against the shared hackathon Apertus endpoint's
  rate limit (a single uncontended call took ~1.8s in manual testing) —
  don't read it as the latency Clausurus adds per chat message in
  production.

Full per-type table and the one leaked span above: `eval/results.md`.

This is Clausurus' own eval on Clausurus' own synthetic dataset — a
development-time signal, not an independent audit. The dataset is
templated and may not reflect the diversity of real correspondence;
false-negative rates on real-world text are likely higher than shown here.

## Development

```bash
# regenerate the eval dataset (deterministic, seeded)
python eval/gen_dataset.py

# run the eval (rules-only needs no key/network)
python eval/run_eval.py --rules-only
python eval/run_eval.py --apertus-key YOUR_KEY   # or set APERTUS_API_KEY

# tests (fully mocked, no network/keys needed)
pytest tests/ -v

# demo: side-by-side original vs. what's actually sent
cd demo && python show_redaction.py letter_de.txt --no-apertus
```

## License

MIT, same as the rest of this repository.
