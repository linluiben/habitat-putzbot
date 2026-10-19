"""Alle Notion-Zugriffe: Queries, Lookups, Schreiboperationen."""

import re
import time
import unicodedata

import requests

import cycles
from config import (
    DRY_RUN,
    DS_A_ID,
    DS_B_ID,
    EMAIL_DOMAIN,
    HEADERS,
    NOTION_API,
    PUTZPLAN_RELATION_PROP,
    PUTZSTATUS_ELIGIBLE,
    TEMPLATE_ID,
    WEEK_STATUS_PLANNED,
    debug,
)


def clean_string(text):
    """Kleinschreibung ohne Umlaute/Diakritika — für generierte E-Mail-Adressen.

    Zwei Dinge, die in der echten Mitgliederliste vorkommen und die Adresse
    sonst unbrauchbar machen:

    - **Spitznamen in Klammern.** „Jacqueline (Jacky)" ergab
      `jacqueline(jacky).hoeger@…`. Der Klammerteil fliegt raus.
    - **Leerzeichen.** „van de Ven" ergab `remco.van de ven@…`, und ein
      Leerzeichen ist in einer Adresse schlicht ungültig.

    Beides macht die Ableitung nur *plausibel*, nicht *richtig* — verlässlich
    wird es erst über `Interne Email`.
    """
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.lower()
    for umlaut, replacement in {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}.items():
        text = text.replace(umlaut, replacement)
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8")
    return "".join(text.split())


# --- Low-Level ---

# Notion steht hinter Cloudflare und liefert ab und zu ein kurzes 502/503/504
# oder ein 429 — ein einziger solcher Treffer hat schon einen ganzen
# Montagslauf abgebrochen. Deshalb wird jeder Aufruf ein paar Mal wiederholt.
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = (2, 5, 15)


def _request(method, url, wiederholbar=True, **kwargs):
    """HTTP-Aufruf an Notion, mit Wiederholung bei vorübergehenden Fehlern.

    `wiederholbar=False` für Aufrufe, die nicht idempotent sind (Seite
    anlegen): ein 502 heißt nicht, dass Notion die Seite *nicht* angelegt hat,
    und ein zweiter Versuch ergäbe dann eine doppelte Woche. Dort wird nur bei
    429 wiederholt — das sagt verbindlich, dass nichts passiert ist.
    """
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 60)

    for versuch, warten in enumerate((*RETRY_WAITS, None), start=1):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            if not wiederholbar or warten is None:
                raise
            print(f"   ⚠️ Notion nicht erreichbar ({e.__class__.__name__}), Versuch {versuch} — neuer Versuch in {warten}s.")
            time.sleep(warten)
            continue

        nochmal = response.status_code == 429 or (wiederholbar and response.status_code in RETRY_STATUS)
        if not nochmal or warten is None:
            return response

        if response.status_code == 429:
            try:
                warten = max(warten, float(response.headers.get("Retry-After", 0)))
            except ValueError:
                pass
        print(f"   ⚠️ Notion antwortet {response.status_code}, Versuch {versuch} — neuer Versuch in {warten:g}s.")
        time.sleep(warten)


def query_data_source(ds_id, filter_payload=None):
    """Query mit Pagination — Notion liefert max. 100 Seiten pro Request."""
    url = f"{NOTION_API}/data_sources/{ds_id}/query"
    results = []
    cursor = None

    while True:
        payload = {}
        if filter_payload:
            payload["filter"] = filter_payload
        if cursor:
            payload["start_cursor"] = cursor

        response = _request("POST", url, json=payload)
        if response.status_code != 200:
            print(f"❌ Notion-Query fehlgeschlagen ({ds_id}): {response.text}")
            return None

        data = response.json()
        results.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    debug(f"Query auf {ds_id}: {len(results)} Seiten geladen.")
    return results


def _prop(props, name, kind, default=None):
    """Wert einer Property lesen, ohne bei fehlender Property zu knallen."""
    return props.get(name, {}).get(kind, default)


def _title_text(props):
    title_prop = next((v for v in props.values() if v.get("type") == "title"), None)
    if not title_prop or not title_prop.get("title"):
        return None
    return title_prop["title"][0]["text"]["content"]


def _relation_ids(props, name):
    relation = _prop(props, name, "relation", []) or []
    if props.get(name, {}).get("has_more"):
        debug(f"⚠️ Relation '{name}' hat mehr als 25 Einträge — Rest wird nicht gelesen.")
    return [item["id"] for item in relation]


# --- Putzplan (DS_B) ---

def get_week_pages():
    """Alle Putzplan-Seiten als {(kw, jahr): seiten-dict}.

    Ein einziger Query statt einer Abfrage pro Woche — wird sowohl für den
    Wochen-Lookup als auch für 'wann hat wer zuletzt geputzt' gebraucht.
    """
    pages = query_data_source(DS_B_ID)
    if pages is None:
        return None

    by_week = {}
    by_page_id = {}

    for page in pages:
        props = page["properties"]
        kw = _prop(props, "Kalenderwoche", "number")
        year = _prop(props, "Jahr", "number")

        if kw is None:
            continue
        if year is None:
            debug(f"⚠️ Seite '{_title_text(props)}' hat kein Jahr — wird ignoriert.")
            continue

        # Sammelseiten wie 'Ausgetragen' (KW 0) oder 'Postponed' (KW 54) sind
        # keine echten Wochen. Sie dürfen weder als Putzeinsatz zählen noch in
        # die Abstandsrechnung einfließen — sonst sähe jede:r, der dort geparkt
        # ist, aus wie frisch geputzt.
        if not 1 <= int(kw) <= cycles.iso_weeks_in_year(int(year)):
            debug(f"'{_title_text(props)}' (KW {int(kw)}) ist keine echte Kalenderwoche — ignoriert.")
            continue

        entry = {
            "page_id": page["id"],
            "page_url": page["url"],
            "kw": int(kw),
            "year": int(year),
            "member_ids": _relation_ids(props, "Mitglieder"),
            "status": (_prop(props, "Status", "status") or {}).get("name"),
            "archiv": _prop(props, "Archiv", "checkbox", False),
        }
        entry["member_count"] = len(entry["member_ids"])

        by_week[(entry["kw"], entry["year"])] = entry
        by_page_id[page["id"]] = entry

    return {"by_week": by_week, "by_page_id": by_page_id}


def notion_lookup(kw, year, week_pages):
    """Status einer einzelnen Woche. `page_status` ist 'exists' oder 'empty'."""
    entry = week_pages["by_week"].get((kw, year))
    if not entry:
        return {
            "page_status": "empty",
            "kw": kw,
            "year": year,
            "page_id": None,
            "page_url": None,
            "member_ids": [],
            "member_count": 0,
            "status": None,
            "archiv": False,
        }
    return dict(entry, page_status="exists")


def update_page_members(page_id, member_ids):
    """Mitglieder-Relation einer Wochenseite komplett überschreiben."""
    if DRY_RUN:
        print(f"   🧪 [DRY RUN] würde Seite {page_id} auf {len(member_ids)} Mitglieder setzen.")
        return True

    response = _request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        json={"properties": {"Mitglieder": {"relation": [{"id": mid} for mid in member_ids]}}},
    )
    if response.status_code == 200:
        print(f"   ✅ Seite geupdated ({len(member_ids)} Mitglieder).")
        return True

    print(f"   ❌ Fehler beim Update: {response.text}")
    return False


def create_week_page(kw, year, member_ids, status=WEEK_STATUS_PLANNED):
    """Neue Wochenseite aus dem Template. Gibt (page_id, page_url) zurück."""
    title = f"Putzcrew KW {kw}"

    if DRY_RUN:
        print(f"   🧪 [DRY RUN] würde Seite '{title}' ({year}) mit {len(member_ids)} Mitgliedern anlegen.")
        return None, None

    payload = {
        "parent": {"data_source_id": DS_B_ID},
        "template": {"type": "template_id", "template_id": TEMPLATE_ID},
        # Überschreiben die Werte aus dem Template.
        # 'children' darf NICHT mit rein, solange 'template' genutzt wird!
        "properties": {
            "Titel": {"title": [{"text": {"content": title}}]},
            "Mitglieder": {"relation": [{"id": mid} for mid in member_ids]},
            "Kalenderwoche": {"number": kw},
            "Jahr": {"number": year},
            "Status": {"status": {"name": status}},
        },
    }

    response = _request("POST", f"{NOTION_API}/pages", wiederholbar=False, json=payload)
    if response.status_code == 200:
        data = response.json()
        print(f"   ✅ Seite '{title}' ({year}) aus Template erstellt.")
        return data["id"], data["url"]

    print(f"   ❌ Template-Fehler: {response.text}")
    return None, None


def set_week_status(page_id, status):
    """Status einer Wochenseite setzen (z.B. auf 'Crew voll')."""
    if DRY_RUN:
        print(f"   🧪 [DRY RUN] würde Status von {page_id} auf '{status}' setzen.")
        return True

    response = _request(
        "PATCH",
        f"{NOTION_API}/pages/{page_id}",
        json={"properties": {"Status": {"status": {"name": status}}}},
    )
    if response.status_code != 200:
        print(f"   ⚠️ Status konnte nicht gesetzt werden: {response.text}")
        return False
    return True


# --- Mitgliederliste (DS_A) ---

MEMBER_FILTER = {
    "and": [
        {"property": "Austrittsdatum", "date": {"is_empty": True}},
        {"property": "Onboarding: Status", "select": {"equals": "Erledigt"}},
        # Die Werte müssen exakt so heißen wie die Optionen in Notion — ein
        # `contains` auf eine nicht existierende Option trifft lautlos nichts.
        # Deckungsgleich mit der Notion-Ansicht „Putzen"; `pruefe_filter_optionen`
        # schlägt Alarm, sobald hier etwas nicht mehr zum Schema passt.
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "passiv"}},
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "Fördermitglied"}},
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "gekündigt"}},
        {
            "or": [
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Vereinsmitglied"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Probemitglied"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Probemitglied (+1 Jahr)"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Jugendliches Mitglied"}},
            ]
        },
        # Putzstatus: nur die Werte aus PUTZSTATUS_ELIGIBLE, aktuell allein
        # 'Normal'. 'Ausgetragen'/'Neu'/'Priorität'/'Postponed' fallen raus —
        # und ein LEERER Putzstatus ebenfalls, denn dort hat noch niemand
        # entschieden. Kein `if value is not None` mehr: käme wieder ein None
        # in die Liste, soll Notion laut meckern statt still weniger zu treffen.
        {
            "or": [
                {"property": "Putzstatus", "select": {"equals": value}}
                for value in PUTZSTATUS_ELIGIBLE
            ]
        },
    ]
}


# Für die Tag-Prüfung bewusst ein WEITERER Filter als beim Auslosen: geprüft
# werden soll jede:r, die/der prinzipiell einmal in den Topf kommen kann, nicht
# nur der heutige Topf. Wer heute 'Neu' ist, ist in zwei Monaten 'Normal' — und
# dann soll die E-Mail schon stimmen, statt dass es beim ersten Einsatz auffällt.
TAG_CHECK_FILTER = {
    "and": [
        {"property": "Austrittsdatum", "date": {"is_empty": True}},
        {"property": "Onboarding: Status", "select": {"equals": "Erledigt"}},
        # Die Werte müssen exakt so heißen wie die Optionen in Notion — ein
        # `contains` auf eine nicht existierende Option trifft lautlos nichts.
        # Deckungsgleich mit der Notion-Ansicht „Putzen"; `pruefe_filter_optionen`
        # schlägt Alarm, sobald hier etwas nicht mehr zum Schema passt.
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "passiv"}},
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "Fördermitglied"}},
        {"property": "Mitgliedsstatus", "multi_select": {"does_not_contain": "gekündigt"}},
        {
            "or": [
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Vereinsmitglied"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Probemitglied"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Probemitglied (+1 Jahr)"}},
                {"property": "Mitgliedsstatus", "multi_select": {"contains": "Jugendliches Mitglied"}},
            ]
        },
        # Putzstatus: nur 'Ausgetragen' müssen wir hier rausfiltern
        {"property": "Putzstatus", "select": {"does_not_equal": "Ausgetragen"}},
    ]
}


# Alle Vergleichsoperatoren, die einen Auswahl-*Namen* nennen. `does_not_equal`
# gehört unbedingt dazu: `TAG_CHECK_FILTER` schließt damit 'Ausgetragen' aus, und
# ein ausschließender Filter auf eine umbenannte Option ist genauso still kaputt
# wie ein einschließender — nur andersherum, dann rutschen plötzlich Leute rein.
AUSWAHL_OPERATOREN = ("equals", "does_not_equal", "contains", "does_not_contain")


def _filter_auswahlwerte(filter_payload):
    """Alle (Property, Wert)-Paare einsammeln, die ein Filter an Auswahlfeldern prüft."""
    treffer = set()

    def durchgehen(knoten):
        if isinstance(knoten, dict):
            prop = knoten.get("property")
            for typ in ("select", "multi_select"):
                bedingung = knoten.get(typ)
                if prop and isinstance(bedingung, dict):
                    for schluessel in AUSWAHL_OPERATOREN:
                        if schluessel in bedingung:
                            treffer.add((prop, bedingung[schluessel]))
            for wert in knoten.values():
                durchgehen(wert)
        elif isinstance(knoten, list):
            for wert in knoten:
                durchgehen(wert)

    durchgehen(filter_payload)
    return treffer


def pruefe_filter_optionen():
    """Prüfen, ob die Auswahlwerte aus den Filtern in Notion überhaupt existieren.

    Notion-Filter sind an *Namen* gebunden, nicht an IDs: wird eine Option
    umbenannt, trifft `contains` einfach nichts mehr — ohne Fehlermeldung, nur
    mit einem stillschweigend kleineren Kandidatenpool. Genau das ist mit
    „Vorläufiges Mitglied" → „Probemitglied" passiert. Diese Prüfung macht so
    etwas sofort sichtbar, statt es einen Auslosungslauf lang zu verschlucken.
    """
    response = _request("GET", f"{NOTION_API}/data_sources/{DS_A_ID}")
    if response.status_code != 200:
        # Die Prüfung selbst darf den Lauf nicht aufhalten.
        print(f"   ⚠️ Schema der Mitgliederliste nicht lesbar: {response.text}")
        return True

    vorhanden = {}
    for name, prop in (response.json().get("properties") or {}).items():
        typ = prop.get("type")
        if typ in ("select", "multi_select"):
            vorhanden[name] = {o["name"] for o in (prop[typ] or {}).get("options", [])}

    fehlend = [
        (prop, wert)
        for prop, wert in sorted(
            _filter_auswahlwerte(MEMBER_FILTER) | _filter_auswahlwerte(TAG_CHECK_FILTER)
        )
        if prop in vorhanden and wert not in vorhanden[prop]
    ]

    if not fehlend:
        debug("Alle Auswahlwerte der Filter existieren in Notion.")
        return True

    print("❌ Der Filter nennt Auswahlwerte, die es in Notion nicht (mehr) gibt:")
    for prop, wert in fehlend:
        print(f"   • {prop}: '{wert}'")
        print(f"     vorhanden: {', '.join(sorted(vorhanden[prop]))}")
    print("   Vermutlich in Notion umbenannt. Bitte notion.py angleichen — sonst")
    print("   lost der Bot aus einem stillschweigend zu kleinen Topf.")
    return False


def get_taggable_members():
    """Alle, die prinzipiell einmal gelost werden können — Grundlage der Tag-Prüfung."""
    members = _load_members(TAG_CHECK_FILTER)
    if members is not None:
        debug(f"{len(members)} Mitglieder mit losbarem Putzstatus (Tag-Prüfung).")
    return members


def get_eligible_members():
    """Losbare Mitglieder — gefiltert nach Mitgliedsstatus, Onboarding und Putzstatus."""
    members = _load_members(MEMBER_FILTER)
    if members is not None:
        debug(f"{len(members)} losbare Mitglieder nach Notion-Filter.")
    return members


def get_all_members():
    """Alle Mitglieder, ungefiltert.

    Gebraucht, um Namen/E-Mails von Leuten aufzulösen, die sich freiwillig
    eingetragen haben, aber selbst nicht losbar sind (z.B. Putzstatus
    'Ausgetragen') — sonst könnte der Bot sie nicht in Slack erwähnen.
    """
    return _load_members(None)


def _load_members(filter_payload):
    pages = query_data_source(DS_A_ID, filter_payload)
    if pages is None:
        return None

    members = []
    for page in pages:
        props = page["properties"]

        full_name = _title_text(props)
        if not full_name:
            continue

        # 'Interne Email' bevorzugen, sonst aus "Nachname, Vorname" ableiten.
        # Die Herkunft wird mitgeführt: eine abgeleitete Adresse ist eine
        # Vermutung und die häufigste Ursache dafür, dass jemand keine DM
        # bekommt. Der Modus `tags` wertet das aus.
        email = _prop(props, "Interne Email", "email")
        email_quelle = "Interne Email" if email else None
        if not email and "," in full_name:
            nachname, vorname = (part.strip() for part in full_name.split(",", 1))
            email = f"{clean_string(vorname)}.{clean_string(nachname)}@{EMAIL_DOMAIN}"
            email_quelle = "abgeleitet"

        eintritt = _prop(props, "Eintrittsdatum", "date") or {}

        members.append(
            {
                "id": page["id"],
                "name": full_name,
                "email": email,
                "email_quelle": email_quelle,
                "eintrittsdatum": eintritt.get("start"),
                "putzstatus": (_prop(props, "Putzstatus", "select") or {}).get("name"),
                "putz_page_ids": _relation_ids(props, PUTZPLAN_RELATION_PROP),
                "extra_weeks": [],  # in diesem Lauf vergebene Einsätze, s. raffle.enrich_members
            }
        )

    return members
