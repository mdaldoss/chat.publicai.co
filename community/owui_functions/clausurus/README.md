# Clausurus 🔒

An Open WebUI **Pipe** function for sending a message to an external,
bring-your-own-key LLM (OpenAI, Gemini, Anthropic or OpenRouter) with personal
details hidden first. It adds four entries to the model menu, *Clausurus 🔒 ·
OpenAI*, *· Gemini*, *· Anthropic* and *· OpenRouter*, next to the Apertus models
users already have.

Built for the Swiss {ai} Weeks "Public AI" challenge.

![Review dialog](demo/screenshots/review-dialog.png)

## What it does

1. **Finds personal data** in every message of the conversation (the whole history
   is sent to the provider on each turn, so it is all redacted each time), in two
   layers:
   - **Rules**, in three groups:
     - *Swiss formats*: AHV/AVS numbers, IBANs, UID/CHE numbers and payment cards
       (all checksum-validated), Swiss phone numbers, vehicle plates, Swiss-style
       street addresses and 4-digit postcodes, and names after a title ("Frau",
       "M.", "Sig.ra", "Mr").
     - *International formats*: emails, international and national phone numbers,
       IPv4/IPv6, GPS coordinates, number-first addresses ("12 rue de la Paix",
       "221 Baker Street"), 5-digit and UK postcodes, and identifier-looking tokens
       (9+ digit numbers, `123-45-6789`, `XK4471920`).
     - *Labelled fields*, for pasted forms, tables and exports: the value after a
       label such as *Passport*, *Ausweis*, *codice fiscale*, *Username*,
       *mot de passe*, *date of birth*, *Nachname* or *Adresse*, in plain text,
       Markdown, JSON, XML or HTML tables, in EN/DE/FR/IT.
   - **Apertus**: finds names, organisations, places, ID numbers, usernames,
     passwords, birth dates, and *contextual* identifiers that rules can't, such
     as "the only pharmacist in the village" or "the mayor's wife". Its answers get
     the same sanity checks as the rules. Apertus is only asked *what to hide*; it
     never answers the question.
2. **Lets you review it** (on by default, per user). Before anything is sent, a
   dialog shows your message with every finding highlighted, what found it (rules,
   Apertus or you), and where the data goes. You can:
   - click a highlight or untick a row to send it as written (false positive);
   - type a phrase, or select text and press *Hide selected text*, to hide
     something that was missed;
   - switch the preview to *what the provider receives*;
   - cancel. Nothing is sent unless you press *Send*.

   Your choices are remembered for the rest of the chat and shown again (and can be
   reversed) the next time.
3. **Replaces** each hidden value with a placeholder (`[PERSON_1]`, `[AHV_1]`,
   `[CONTEXT_1: a specific local individual]`, ...), consistently across the
   conversation, and tells the model to keep placeholders as they are.
4. **Sends** the redacted conversation with the user's own API key.
5. **Restores** the real values in the reply as it streams in.
6. **Adds a privacy report** under the reply (optional, per user): each hidden
   value, the placeholder the provider saw, what found it, what you chose to send
   as written, and where the data went.

![What the provider receives](demo/screenshots/review-what-provider-receives.png)

![Privacy report](demo/screenshots/privacy-report.png)

### Where your data goes

Both the dialog and the report show the path a message takes and what each hop
sees:

| Hop | Where | Sees |
|---|---|---|
| You | Your device | Original text |
| Open WebUI | Set by the admin (`server_location`); chat.publicai.co runs in AWS eu-central-2, Zürich | Original text |
| Apertus | Set by the admin (`apertus_location`); default endpoint verified in Zürich, Switzerland (Swisscom, AS3303) | Original text, to find what to hide; returns a list only |
| External provider | Company jurisdiction: 🇺🇸 United States for all four | Redacted text only |

The external providers are shown by **company jurisdiction**, not server location:
their API hostnames resolve to anycast networks (Cloudflare, Google, Anthropic) and
they don't disclose which region serves a given request. Locations are labels an
admin sets, not live measurements; update them if you change the endpoints.

## Threat model: read this before relying on it

**What Clausurus does**: stops the chosen external provider from seeing the
personal details it finds (or that you mark), and shows you exactly what those are.

**What Clausurus does not do**:

- **It does not guarantee anonymity.** Detection misses things (see the eval
  below), and the details left in the text can still point to a person: a rare
  job, a small village, a specific date.
- **Pseudonymised text can still be personal data.** Under the Swiss FADP and the
  GDPR, text with identifiers replaced by placeholders is not automatically
  anonymous, especially while the mapping back to real values exists (it does,
  here, for the duration of the request) or when the person is identifiable from
  context.
- **It is not a compliance certification.** Nobody has audited it against the FADP,
  the GDPR or any standard. It is a best-effort technical control.
- **Apertus sees the original text.** To find what to hide, the full message goes
  to the configured Apertus endpoint (by default the Swiss AI Weeks endpoint
  hosted by Swisscom in Zürich). Point `apertus_api_base` at an Apertus deployment
  you trust, for example the platform's own.
- **The Open WebUI server sees the original text**, as it does for every chat.
- **Text only.** Images and files can't be redacted, so they are not sent to the
  provider unless an admin enables `allow_images`.
- **Apertus output is not trusted blindly.** Only spans that appear word for word
  in your message are used, so a confused or manipulated Apertus answer can't
  inject text. It can still *miss* things; the rules layer and the review dialog
  are the backstop.
- **If Apertus fails**, the request is blocked by default rather than sent with
  less redaction (configurable).

## Settings

### Admin valves

| Valve | Default | Purpose |
|---|---|---|
| `apertus_api_base` | Swiss AI Weeks endpoint | OpenAI-compatible Apertus endpoint, used only for detection |
| `apertus_api_key` | *(empty)* | Key for that endpoint |
| `apertus_model` | `swiss-ai/Apertus-v1.5-70B` | Model name at that endpoint |
| `apertus_location` | `🇨🇭 Zürich, Switzerland · Swisscom` | Shown in the data-flow view |
| `server_location` | *(generic label)* | Where this Open WebUI runs, shown in the data-flow view |
| `enable_apertus` | `true` | Off = rules and checksums only |
| `apertus_structured_call` | `true` | Second, parallel Apertus call for ID numbers, usernames, passwords and birth dates; off halves Apertus usage |
| `block_on_apertus_failure` | `true` | Block the request if Apertus fails, instead of rules-only with a warning |
| `allow_images` | `false` | Send image/file parts unredacted |
| `review_timeout_seconds` | `300` | Close the review dialog (and cancel) after this |
| `*_api_key` | *(empty)* | Admin fallback keys, for demos only; every user would spend them |
| `*_model` | `gpt-4.1-mini`, `gemini-2.5-flash`, `claude-sonnet-5`, `openai/gpt-4.1-mini` | Model per provider |

### User settings (Settings → Functions → Clausurus)

| Setting | Default | Purpose |
|---|---|---|
| `review_before_sending` | on | Show the review dialog before each message |
| `show_privacy_report` | on | Add the privacy report under each reply |
| `openai_api_key`, `gemini_api_key`, `anthropic_api_key`, `openrouter_api_key` | *(empty)* | Your own keys. A user key always wins over an admin fallback key |

## Limits

- **Detection misses contextual phrases.** About 1 in 8 in the Swiss eval, and 8
  of 20 in a separate development set of letters. In live testing, "Die Frau des
  Gemeindepräsidenten" in the demo letter was flagged only partly or not at all,
  depending on the run. Keep the review step on when it matters.
- **Some harmless values get hidden.** Numbers of 9+ digits (a ticket number),
  upper-case codes with 3+ digits, anything after a field label like `Name:` or
  `ID:`, "1000 Personen" (read as postcode + town), and occasionally an amount
  flagged by Apertus ("CHF 1'250.50", 3 of 24 test requests). The review dialog
  lets you send these as written.
- **Two Apertus calls per message** (one for people, places and descriptions, one
  for ID numbers, usernames, passwords and birth dates) run in parallel, so latency
  barely changes, but usage doubles. On the shared hackathon endpoint this hit the
  rate limit in the benchmark (14 of 200 documents fell back to rules only). Turn
  off `apertus_structured_call` to halve usage.
- **Review decisions live in server memory**, per user and chat. After a restart,
  a function update, or on another replica, they are forgotten: things you chose to
  send as written get hidden again (safe), but **phrases you added are no longer
  hidden automatically** until you add them again. With the review step on you see
  this in the dialog; with it off you don't.
- **The review dialog is JavaScript that runs in the Open WebUI page** (Open WebUI's
  `execute` event). That is how Open WebUI lets functions show custom UI, and it
  means admins should only install this function from a source they trust. The
  dialog inserts all message text as plain text, never as HTML.
- **Title, tag and follow-up generation** go through Clausurus too when it is the
  selected model. They are redacted the same way, silently (no dialog or report).
- **Formats**: rules cover Swiss formats and common international and labelled
  formats in EN/DE/FR/IT; unlabelled values in other countries' formats rely on
  Apertus.
- **Restoring** is plain text replacement. If the model rephrases a placeholder
  (`[Person 1]`), that value stays a placeholder in the reply.

## Evaluation

`eval/benchmark.py` scores what each system hides, regardless of the label it uses:
an identifier counts as caught if any hidden span overlaps it. It compares Clausurus
with Microsoft [Presidio](https://github.com/microsoft/presidio) (spaCy large models
for EN/DE/FR/IT, its predefined recognizers, no customisation) on three sets. Full
tables: [`eval/benchmark_results.md`](eval/benchmark_results.md).

- **Swiss set**: 40 synthetic letters and emails (DE/FR/IT/EN) with 172 identifiers
  (`eval/gen_dataset.py`), the use case Clausurus is built for, written by us.
- **ai4privacy**: 200 random documents (50 per language, seed 13) from the
  validation split of [ai4privacy/pii-masking-300k](https://huggingface.co/datasets/ai4privacy/pii-masking-300k),
  876 direct identifiers. External, international, mostly forms and records.
- **False alarms**: 24 ordinary chatbot requests with no personal data
  (`eval/benign.json`).

| | Swiss set: caught | ai4privacy: caught (95% CI) | ai4privacy: precision | False alarms (of 24 requests) |
|---|---|---|---|---|
| Clausurus v0.2, rules only | 63% | 12% (9–15%) | 96% | not measured |
| Clausurus v0.2, rules + Apertus | 98% | 60% (54–66%) | 90% | not measured |
| **Clausurus v0.3, rules only** | 63% | **70% (65–75%)** | 95% | **0** |
| **Clausurus v0.3, rules + Apertus** | **98%** (3 of 172 leaked) | **88.5% (85–91%)** | 90% | 3 requests (amounts) |
| Presidio, all results | 70% | 48% (44–52%) | 69% | 14 requests (25 items) |
| Presidio, score ≥ 0.5 | 61% | 39% (36–43%) | 66% | 14 requests (24 items) |

Time per document in the benchmark: Presidio ~25 ms, Clausurus rules ~1 ms,
Clausurus + Apertus 8–9 s. That last figure includes rate-limit waits on the shared
endpoint while processing 3 documents at a time. A single message (the demo letter)
took about 2 s with one or two Apertus calls, but 33 s once, right after the benchmark
had used up the endpoint's rate limit: expect spikes on a shared endpoint.

### How v0.3 was developed

v0.2 only knew Swiss formats, and its Apertus prompt only asked for names, places and
contextual descriptions, so on ai4privacy 40% of direct identifiers leaked (mostly ID
numbers, usernames, passwords, birth dates and addresses in forms). v0.3 adds
international formats, labelled fields and a second Apertus call for those types.

To keep the numbers honest:

- Rules were developed on 400 documents from the ai4privacy **training** split. The
  200 validation documents above were only scored once v0.3 was finished, the same
  documents as for v0.2.
- The Apertus prompt split was chosen on a separate development set
  (`eval/contextual_dev.json`, 20 contextual phrases not used elsewhere). One
  combined prompt caught 2/20 of them inside letters; two prompts caught 12/20.
  The first v0.3 attempt with the combined prompt dropped the Swiss set's
  contextual recall from 88% to 17%, which is how this was found.
- The false-alarm set was written before the v0.3 rules. It was then used once to
  add sanity checks to Apertus answers (a password can't contain spaces, every part
  of an ID must contain a digit), so it is no longer fully independent.
- An earlier v0.2 number (1/172 leaked on the Swiss set) was inflated: the prompt
  then used two example phrases that also appear in the Swiss set.

### What the numbers do and don't say

- **On the Swiss set Clausurus has home advantage**: we wrote the data, and Presidio
  has no AHV recognizer (0%) and hardly handles contextual descriptions (4%).
- **ai4privacy is the fairer test.** It is still synthetic, LLM-generated and heavy
  on form-like records; real letters and chats will look different.
- **Presidio is ~150× faster and needs no external service.** It can also be given
  custom recognizers; this compares the out-of-the-box setup.
- Clausurus hides quasi-identifiers only partly (cities 45%, other dates 14%, sex and
  title 48%), by design: hiding every date and place makes answers useless.
- ai4privacy is used under its academic / non-commercial license: downloaded at run
  time, not stored here, aggregate numbers only.

`eval/run_eval.py` keeps the older per-type report for the Swiss set
([`eval/results.md`](eval/results.md)).

## Try it

**Tests** (mocked, no network or keys):

```bash
pytest community/owui_functions/clausurus/tests -v
```

**See the redaction without Open WebUI:**

```bash
cd community/owui_functions/clausurus/demo
python show_redaction.py letter_de.txt --no-apertus        # rules only
APERTUS_API_KEY=... python show_redaction.py letter_de.txt  # rules + Apertus
```

**Full UI with Docker:**

```bash
cd community/owui_functions/clausurus/demo
cp .env.example .env   # fill in WEBUI_SECRET_KEY, APERTUS_API_KEY and one provider key
docker compose up
# open http://localhost:3000, log in as admin@example.com / clausurus-demo-only,
# pick "Clausurus 🔒 · <provider>" in the model menu
```

**Full UI without Docker** (what was used to test this function):

```bash
pip install open-webui==0.11.3
WEBUI_SECRET_KEY=change-me open-webui serve --port 8080 &
cd community/owui_functions/clausurus/demo
WEBUI_BASE_URL=http://localhost:8080 APERTUS_API_KEY=... OPENROUTER_API_KEY=... \
  python install_function.py
```

**On an existing Open WebUI**: Admin Panel → Functions → + → paste `clausurus.py`,
enable it, then set the Apertus key and locations in its valves. Each user adds
their own provider key in Settings → Functions → Clausurus.

**Eval:**

```bash
python eval/gen_dataset.py                    # regenerate the dataset
python eval/run_eval.py --rules-only          # no key needed
APERTUS_API_KEY=... python eval/run_eval.py   # rules + Apertus (~4 min, rate-limited)

# Clausurus vs. Presidio on the Swiss set, ai4privacy and the false-alarm set (needs
# presidio-analyzer and the four spaCy *_lg models; downloads the ai4privacy validation
# split to ~/.cache; ~30 min with Apertus because of rate limits)
APERTUS_API_KEY=... python eval/benchmark.py --per-language 50
# development data only (training split), for working on the rules
python eval/benchmark.py --datasets ai4privacy-dev benign --systems clausurus-rules --per-language 100
```

## License

MIT, like the rest of this repository.
