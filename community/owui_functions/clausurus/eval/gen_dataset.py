"""
Generates the 40-text synthetic Swiss evaluation set for Clausurus.

Every person, address, AHV number, IBAN, phone number etc. below is
fictional. AHV numbers and IBANs are generated so their check digits are
mathematically valid (to test recognizer + checksum logic against realistic
formats), but the underlying numbers are randomly drawn and do not belong
to any real person or account.

Run:
    python gen_dataset.py

Writes eval/data/<id>.txt (the raw text) and eval/data/<id>.json (gold
entity spans: start, end, text, type) for 40 texts across
DE/FR/IT/EN x {citizen complaint, commune correspondence, HR email,
bank-style letter}.
"""

import json
import random
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

random.seed(42)

# --------------------------------------------------------------------------
# Fake data generators with valid checksums
# --------------------------------------------------------------------------

FIRST_NAMES = {
    "de": ["Heidi", "Urs", "Verena", "Beat", "Priska", "Res", "Silvia", "Werner", "Katharina", "Fritz"],
    "fr": ["Chantal", "Didier", "Nathalie", "Bertrand", "Sylvie", "Gregoire", "Isabelle", "Marc", "Odile", "Yves"],
    "it": ["Rosanna", "Marco", "Ivana", "Gianfranco", "Patrizia", "Renzo", "Simona", "Aldo", "Franca", "Nello"],
    "en": ["Emily", "Jonathan", "Rachel", "Andrew", "Megan", "Kevin", "Louise", "Peter", "Susan", "Graham"],
}
LAST_NAMES = {
    "de": ["Beispielmann", "Mustermann", "Zurbriggen", "Steingruber", "Hodel", "Wyss", "Oberholzer"],
    "fr": ["Exemplier", "Dupont", "Bochud", "Fontanet", "Pillonel", "Rossier", "Maillard"],
    "it": ["Esempi", "Bianchi", "Ferrari", "Conti", "Bettoni", "Rossetti", "Galli"],
    "en": ["Example", "Sampleton", "Fairweather", "Ashworth", "Bramley", "Cotterill"],
}
STREETS = {
    "de": ["Musterweg", "Bahnhofstrasse", "Seestrasse", "Dorfgasse", "Kirchweg"],
    "fr": ["Rue de l'Exemple", "Chemin des Fleurs", "Avenue de la Gare", "Rue du Lac"],
    "it": ["Via Modello", "Via della Stazione", "Via del Lago", "Piazza del Mercato"],
    "en": ["Example Road", "Station Avenue", "Lakeside Lane", "Church Close"],
}
TOWNS = [
    ("9999", "Fantasiedorf"), ("1000", "Faketown"), ("2000", "Villefictive"),
    ("6999", "Paesefinto"), ("8999", "Musterhausen"), ("3999", "Beispieldorf"),
]
COMMUNES = ["Gemeinde Fantasiedorf", "Commune de Villefictive", "Comune di Paesefinto", "Municipality of Faketown"]
ORGS = {
    "de": ["Einwohnerdienste", "Sozialamt", "Steueramt", "Bauverwaltung"],
    "fr": ["Service des habitants", "Service social", "Service des contributions"],
    "it": ["Ufficio abitanti", "Ufficio sociale", "Ufficio imposte"],
    "en": ["Residents' Office", "Social Services", "Tax Office"],
}
CONTEXTUAL_TEMPLATES = {
    "de": [
        "die einzige Apothekerin im Dorf",
        "die Frau des Gemeindepräsidenten",
        "der neue Hauswart im Blockhaus 4",
        "mein Nachbar im dritten Stock",
    ],
    "fr": [
        "la seule pharmacienne du village",
        "l'épouse du président de la commune",
        "le nouveau concierge de l'immeuble 4",
        "mon voisin du troisième étage",
    ],
    "it": [
        "l'unica farmacista del paese",
        "la moglie del sindaco",
        "il nuovo portinaio del palazzo 4",
        "il mio vicino al terzo piano",
    ],
    "en": [
        "the only pharmacist in the village",
        "the mayor's wife",
        "the new caretaker of building 4",
        "my neighbour on the third floor",
    ],
}


def gen_ahv() -> str:
    body = "756" + "".join(random.choices("0123456789", k=9))
    nums = [int(d) for d in body]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(nums))
    check = (10 - total % 10) % 10
    full = body + str(check)
    return f"{full[0:3]}.{full[3:7]}.{full[7:11]}.{full[11:13]}"


def gen_iban() -> str:
    bank = "".join(random.choices("0123456789", k=5))
    account = "".join(random.choices("0123456789", k=12))
    bban = bank + account
    rearranged = bban + "CH" + "00"
    converted = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - (int(converted) % 97)
    return f"CH{check:02d}{bban}"


def gen_phone() -> str:
    return f"+41 79 {random.randint(100,999)} {random.randint(10,99)} {random.randint(10,99)}"


def gen_email(first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}@example-mail.ch"


def gen_address(lang: str):
    street = random.choice(STREETS[lang])
    num = random.randint(1, 199)
    plz, town = random.choice(TOWNS)
    return f"{street} {num}, {plz} {town}"


# --------------------------------------------------------------------------
# Text templates: (lang, category, template_fn)
# Each template_fn(rng_seed) -> (text, gold_entities: list[(text, type)])
# Gold entity `text` must appear verbatim in the generated text.
# --------------------------------------------------------------------------


def citizen_complaint(lang: str, idx: int):
    first = random.choice(FIRST_NAMES[lang])
    last = random.choice(LAST_NAMES[lang])
    name = f"{first} {last}"
    address = gen_address(lang)
    ahv = gen_ahv()
    phone = gen_phone()
    contextual = random.choice(CONTEXTUAL_TEMPLATES[lang])

    templates = {
        "de": (
            f"Sehr geehrte Damen und Herren,\n\n"
            f"ich, Frau {name}, wohnhaft an der {address}, wende mich mit einer Beschwerde an Sie.\n"
            f"Meine AHV-Nummer lautet {ahv}. Sie erreichen mich unter {phone}.\n\n"
            f"Seit Wochen parkiert {contextual} regelmässig vor meiner Einfahrt und blockiert den Zugang.\n"
            f"Ich bitte Sie höflich um Klärung.\n\nFreundliche Grüsse\n{name}"
        ),
        "fr": (
            f"Madame, Monsieur,\n\n"
            f"Je soussignée, Mme {name}, domiciliée {address}, me permets de vous adresser une réclamation.\n"
            f"Mon numéro AVS est {ahv}. Vous pouvez me joindre au {phone}.\n\n"
            f"Depuis plusieurs semaines, {contextual} stationne devant mon entrée et en bloque l'accès.\n"
            f"Je vous prie de bien vouloir clarifier la situation.\n\nMeilleures salutations\n{name}"
        ),
        "it": (
            f"Gentili Signore e Signori,\n\n"
            f"la sottoscritta, Sig.ra {name}, residente in {address}, desidera presentare un reclamo.\n"
            f"Il mio numero AVS è {ahv}. Potete contattarmi al {phone}.\n\n"
            f"Da alcune settimane {contextual} parcheggia regolarmente davanti al mio accesso.\n"
            f"Vi chiedo cortesemente di chiarire la situazione.\n\nCordiali saluti\n{name}"
        ),
        "en": (
            f"Dear Sir or Madam,\n\n"
            f"I, Mrs {name}, residing at {address}, would like to raise a complaint.\n"
            f"My social security (AHV) number is {ahv}. You can reach me at {phone}.\n\n"
            f"For several weeks now, {contextual} has been parking in front of my driveway.\n"
            f"I kindly ask you to look into this matter.\n\nKind regards\n{name}"
        ),
    }
    text = templates[lang]
    gold = [
        (name, "PERSON"),
        (address, "ADDRESS"),
        (ahv, "AHV"),
        (phone, "PHONE"),
        (contextual, "CONTEXTUAL"),
    ]
    return text, gold


def commune_correspondence(lang: str, idx: int):
    first = random.choice(FIRST_NAMES[lang])
    last = random.choice(LAST_NAMES[lang])
    name = f"{first} {last}"
    address = gen_address(lang)
    commune = random.choice(COMMUNES)
    org = random.choice(ORGS[lang])
    email = gen_email(first, last)
    contextual = random.choice(CONTEXTUAL_TEMPLATES[lang])

    templates = {
        "de": (
            f"{commune} – {org}\n\n"
            f"Betrifft: Ummeldung von {name}\n\n"
            f"Wir bestätigen den Eingang Ihrer Anmeldung an der neuen Adresse {address}.\n"
            f"Bitte bestätigen Sie den Erhalt per E-Mail an {email}.\n"
            f"Zur Klärung eines offenen Punktes hat sich {contextual} ebenfalls bei uns gemeldet.\n\n"
            f"Freundliche Grüsse\n{org}"
        ),
        "fr": (
            f"{commune} – {org}\n\n"
            f"Concerne: changement d'adresse de {name}\n\n"
            f"Nous confirmons la réception de votre annonce à la nouvelle adresse {address}.\n"
            f"Merci de confirmer par courriel à {email}.\n"
            f"Pour clarifier un point en suspens, {contextual} nous a également contactés.\n\n"
            f"Meilleures salutations\n{org}"
        ),
        "it": (
            f"{commune} – {org}\n\n"
            f"Oggetto: cambio di domicilio di {name}\n\n"
            f"Confermiamo la ricezione della sua notifica al nuovo indirizzo {address}.\n"
            f"La preghiamo di confermare via e-mail a {email}.\n"
            f"Per chiarire un punto in sospeso, anche {contextual} ci ha contattato.\n\n"
            f"Cordiali saluti\n{org}"
        ),
        "en": (
            f"{commune} – {org}\n\n"
            f"Subject: change of address for {name}\n\n"
            f"We confirm receipt of your registration at the new address {address}.\n"
            f"Please confirm by email at {email}.\n"
            f"To clarify an open matter, {contextual} has also contacted us.\n\n"
            f"Kind regards\n{org}"
        ),
    }
    text = templates[lang]
    gold = [
        (name, "PERSON"),
        (address, "ADDRESS"),
        (email, "EMAIL"),
        (contextual, "CONTEXTUAL"),
    ]
    return text, gold


def hr_email(lang: str, idx: int):
    first = random.choice(FIRST_NAMES[lang])
    last = random.choice(LAST_NAMES[lang])
    name = f"{first} {last}"
    manager_first = random.choice(FIRST_NAMES[lang])
    manager_last = random.choice(LAST_NAMES[lang])
    manager = f"{manager_first} {manager_last}"
    email = gen_email(first, last)
    phone = gen_phone()
    ahv = gen_ahv()

    templates = {
        "de": (
            f"Betreff: Vertragsunterlagen {name}\n\n"
            f"Hallo {name},\n\n"
            f"anbei die Vertragsunterlagen. Bitte prüfe deine AHV-Nummer {ahv} auf Richtigkeit.\n"
            f"Bei Fragen wende dich an deinen Vorgesetzten {manager} unter {phone} oder {email}.\n\n"
            f"Freundliche Grüsse\nHR-Abteilung"
        ),
        "fr": (
            f"Objet: documents contractuels {name}\n\n"
            f"Bonjour {name},\n\n"
            f"veuillez trouver ci-joint vos documents contractuels. Merci de vérifier votre numéro AVS {ahv}.\n"
            f"Pour toute question, contactez votre responsable {manager} au {phone} ou {email}.\n\n"
            f"Cordialement\nService RH"
        ),
        "it": (
            f"Oggetto: documenti contrattuali {name}\n\n"
            f"Ciao {name},\n\n"
            f"in allegato i documenti contrattuali. Verifica il tuo numero AVS {ahv}.\n"
            f"Per domande contatta il tuo responsabile {manager} al {phone} o {email}.\n\n"
            f"Cordiali saluti\nUfficio HR"
        ),
        "en": (
            f"Subject: contract documents for {name}\n\n"
            f"Hi {name},\n\n"
            f"please find attached your contract documents. Please check your AHV number {ahv}.\n"
            f"For questions, contact your manager {manager} at {phone} or {email}.\n\n"
            f"Kind regards\nHR Department"
        ),
    }
    text = templates[lang]
    gold = [
        (name, "PERSON"),
        (manager, "PERSON"),
        (ahv, "AHV"),
        (phone, "PHONE"),
        (email, "EMAIL"),
    ]
    return text, gold


def bank_letter(lang: str, idx: int):
    first = random.choice(FIRST_NAMES[lang])
    last = random.choice(LAST_NAMES[lang])
    name = f"{first} {last}"
    address = gen_address(lang)
    iban = gen_iban()

    templates = {
        "de": (
            f"Sehr geehrte(r) {name}\n\n"
            f"Wir bestätigen die Eröffnung Ihres Kontos mit der IBAN {iban}.\n"
            f"Ihre hinterlegte Adresse: {address}.\n"
            f"Bitte prüfen Sie die Angaben und melden Sie uns Abweichungen umgehend.\n\n"
            f"Freundliche Grüsse\nIhre Bank"
        ),
        "fr": (
            f"Cher/Chère {name}\n\n"
            f"Nous confirmons l'ouverture de votre compte avec l'IBAN {iban}.\n"
            f"Adresse enregistrée: {address}.\n"
            f"Merci de vérifier ces informations et de nous signaler toute erreur.\n\n"
            f"Meilleures salutations\nVotre banque"
        ),
        "it": (
            f"Gentile {name}\n\n"
            f"Confermiamo l'apertura del suo conto con IBAN {iban}.\n"
            f"Indirizzo registrato: {address}.\n"
            f"La preghiamo di verificare i dati e segnalarci eventuali discrepanze.\n\n"
            f"Cordiali saluti\nLa sua banca"
        ),
        "en": (
            f"Dear {name}\n\n"
            f"We confirm the opening of your account with IBAN {iban}.\n"
            f"Address on file: {address}.\n"
            f"Please verify these details and report any discrepancies promptly.\n\n"
            f"Kind regards\nYour Bank"
        ),
    }
    text = templates[lang]
    gold = [
        (name, "PERSON"),
        (iban, "IBAN"),
        (address, "ADDRESS"),
    ]
    return text, gold


CATEGORIES = [citizen_complaint, commune_correspondence, hr_email, bank_letter]
LANGS = ["de", "fr", "it", "en"]


def compile_gold(text: str, gold_pairs: list[tuple[str, str]]) -> list[dict]:
    spans = []
    for value, etype in gold_pairs:
        idx = text.find(value)
        if idx == -1:
            raise ValueError(f"gold span not found verbatim: {value!r}")
        spans.append({"start": idx, "end": idx + len(value), "text": value, "type": etype})
    spans.sort(key=lambda s: s["start"])
    return spans


def main():
    manifest = []
    counter = 0
    # 4 languages x 4 categories x ~2-3 variants = 40 texts
    variants_per_combo = [3, 3, 2, 2]  # sums to 10 per language -> 40 total
    for lang in LANGS:
        for cat_fn, n_variants in zip(CATEGORIES, variants_per_combo):
            for v in range(n_variants):
                counter += 1
                text, gold_pairs = cat_fn(lang, counter)
                gold = compile_gold(text, gold_pairs)
                doc_id = f"{counter:02d}_{lang}_{cat_fn.__name__}"
                (DATA_DIR / f"{doc_id}.txt").write_text(text, encoding="utf-8")
                (DATA_DIR / f"{doc_id}.json").write_text(
                    json.dumps({"id": doc_id, "lang": lang, "category": cat_fn.__name__, "entities": gold}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                manifest.append(doc_id)

    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Generated {len(manifest)} synthetic texts in {DATA_DIR}")


if __name__ == "__main__":
    main()
