"""Offline-Tests für die Zyklus- und Auslosungslogik.

Läuft ohne Notion/Slack und ohne Env-Variablen — Notion- und Slack-Aufrufe
werden gefaked. Aufruf:

    python tests.py

Deckt bewusst die Fälle ab, die sich in echt schlecht testen lassen:
Jahreswechsel, KW-53-Jahre, leergelaufene Kandidatenpools und die Frage,
ob jemand im selben Zyklus doppelt verplant wird.
"""

import sys
from collections import Counter
from datetime import date, timedelta

import config

config.DEBUG = False

import cycles  # noqa: E402
import notion  # noqa: E402
import raffle  # noqa: E402
import reschedule  # noqa: E402
import scheduler  # noqa: E402
import slack_utils  # noqa: E402
import tagcheck  # noqa: E402
import tracking  # noqa: E402

TODAY = date.today()
FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"  FEHLER {label}: ist {got!r}, erwartet {want!r}")
        print(f"  ✗ {label}")
    else:
        print(f"  ✓ {label}")


def member(mid, count=0, recent=False, new=False, too_close=False, putz_pages=()):
    """Mitglied mit schon berechneten Flags (umgeht enrich_members)."""
    abstand = 2 if too_close else (8 if recent else None)
    return {
        "id": mid,
        "name": f"Nachname{mid}, Vorname{mid}",
        "email": f"{mid}@das-habitat.de",
        "eintrittsdatum": (TODAY - timedelta(days=30 if new else 900)).isoformat(),
        "putzstatus": None,
        "putz_page_ids": list(putz_pages) + ["alt"] * count,
        "extra_weeks": [],
        "putz_count": count + len(putz_pages),
        "naechster_abstand": abstand,
        "ist_kuerzlich_dran": recent or too_close,
        "ist_zu_dicht_dran": too_close,
        "ist_neu": new,
    }


EMPTY_WEEK = {"member_ids": [], "member_count": 0}


# --------------------------------------------------------------- Zyklus-Mathe

def test_cycles():
    print("\n=== Zyklen & Kalenderwochen ===")
    check("2026 ist ein 53-Wochen-Jahr", cycles.iso_weeks_in_year(2026), 53)
    check("2027 ist ein 52-Wochen-Jahr", cycles.iso_weeks_in_year(2027), 52)

    check("KW 1 -> Zyklus 1", cycles.cycle_of_week(1), 1)
    check("KW 5 -> Zyklus 2", cycles.cycle_of_week(5), 2)
    check("KW 53 -> Zyklus 13 (kein 14.)", cycles.cycle_of_week(53), 13)

    check("Zyklus 1/2026", cycles.weeks_in_cycle(1, 2026), [1, 2, 3, 4])
    check("Zyklus 13/2026 schluckt KW 53", cycles.weeks_in_cycle(13, 2026), [49, 50, 51, 52, 53])
    check("Zyklus 13/2027 endet bei 52", cycles.weeks_in_cycle(13, 2027), [49, 50, 51, 52])

    check("KW 4 ist Zyklusende", cycles.is_last_week_of_cycle(4, 2026), True)
    check("KW 52/2026 ist kein Zyklusende", cycles.is_last_week_of_cycle(52, 2026), False)
    check("KW 53/2026 ist Zyklusende", cycles.is_last_week_of_cycle(53, 2026), True)

    check(
        "nach KW 4/2026 kommt KW 5-8",
        cycles.next_cycle_weeks(4, 2026),
        [(5, 2026), (6, 2026), (7, 2026), (8, 2026)],
    )
    check(
        "nach KW 53/2026 kommt KW 1-4/2027",
        cycles.next_cycle_weeks(53, 2026),
        [(1, 2027), (2, 2027), (3, 2027), (4, 2027)],
    )

    check("Abstand über Jahresgrenze", cycles.weeks_between(52, 2025, 1, 2026), 1)
    check("Abstand innerhalb des Jahres", cycles.weeks_between(1, 2026, 13, 2026), 12)


# ------------------------------------------------------------------- Auslosung

def test_raffle():
    print("\n=== Auslosung ===")

    # Wer vor einer Lockerung im Topf war, muss gesetzt bleiben.
    ideal = member("IDEAL")
    others = [member(f"R{i}", recent=True) for i in range(5)]
    picks = Counter()
    for _ in range(300):
        picks.update(m["id"] for m in raffle.select_crew([ideal] + others, EMPTY_WEEK, 2))
    check("Idealkandidat wird immer gesetzt", picks["IDEAL"], 300)
    check("Restplatz wird verlost", len([k for k in picks if k != "IDEAL"]), 5)

    # Alt/Neu-Mischung
    pool = [member(f"N{i}", new=True) for i in range(6)] + [member(f"A{i}") for i in range(6)]
    mixes = Counter()
    for _ in range(300):
        crew = raffle.select_crew(pool, EMPTY_WEEK, 4)
        mixes[sum(1 for m in crew if m["ist_neu"])] += 1
    check("immer 2 neue + 2 alte", dict(mixes), {2: 300})

    # Schon eingetragene Mitglieder
    with_one = {"member_ids": ["N0"], "member_count": 1}
    doubled = any(
        m["id"] == "N0" for _ in range(50) for m in raffle.select_crew(pool, with_one, 3)
    )
    check("bereits Eingetragene werden nicht erneut gezogen", doubled, False)

    mixes2 = Counter()
    for _ in range(200):
        crew = raffle.select_crew(pool, with_one, 3)
        mixes2[1 + sum(1 for m in crew if m["ist_neu"])] += 1
    check("Mischung zählt Eingetragene mit", dict(mixes2), {2: 200})

    # Eskalation der Einsatz-Obergrenze
    check("greift bis ≤3 Einsätze", len(raffle.select_crew([member(f"H{i}", count=3) for i in range(4)], EMPTY_WEEK, 4)), 4)
    check("stoppt oberhalb von 3 Einsätzen", len(raffle.select_crew([member(f"X{i}", count=4) for i in range(4)], EMPTY_WEEK, 4)), 0)

    # Der harte Mindestabstand darf durch keine Fallback-Stufe aufgeweicht werden.
    solo = [member("SOLO")] + [member(f"B{i}", too_close=True) for i in range(8)]
    check(
        "Mindestabstand ist nicht lockerbar",
        [m["id"] for m in raffle.select_crew(solo, EMPTY_WEEK, 4)],
        ["SOLO"],
    )


def test_enrich():
    print("\n=== enrich_members ===")
    week_pages = {
        "by_page_id": {
            "nah": {"kw": 3, "year": 2027, "page_id": "nah"},
            "fern": {"kw": 20, "year": 2026, "page_id": "fern"},
        }
    }
    nah = member("NAH", putz_pages=["nah"])
    fern = member("FERN", putz_pages=["fern"])
    extra = member("EXTRA")
    extra["extra_weeks"] = [(2, 2027)]

    raffle.enrich_members([nah, fern, extra], week_pages, 1, 2027)

    check("Einsatz 2 Wochen entfernt -> hart geblockt", nah["ist_zu_dicht_dran"], True)
    check("Einsatz 33 Wochen entfernt -> frei", fern["ist_zu_dicht_dran"], False)
    check("... und außerhalb der Schonfrist", fern["ist_kuerzlich_dran"], False)
    check("in diesem Lauf vergebener Einsatz blockt", extra["ist_zu_dicht_dran"], True)
    check("... und zählt als Einsatz", extra["putz_count"], 1)


# ---------------------------------------------------------------- Kompletter Lauf

def test_plan_flow():
    print("\n=== Plan-Lauf (Jahreswechsel 2026 -> 2027) ===")

    writes = []
    original = (
        notion.update_page_members,
        notion.set_week_status,
        notion.create_week_page,
        slack_utils.get_slack_user_id,
        slack_utils.post_channel,
        slack_utils.send_dm,
    )

    notion.update_page_members = lambda pid, ids: writes.append(("update", pid, len(ids))) or True
    notion.set_week_status = lambda pid, s: writes.append(("status", pid, s)) or True
    notion.create_week_page = lambda kw, year, ids, status="Geplant": (
        writes.append(("create", kw, year, len(ids), status)) or (f"page-{kw}", f"https://notion.so/kw{kw}")
    )
    slack_utils.get_slack_user_id = lambda email: f"U{email.split('@')[0].upper()}" if email else None
    slack_utils.post_channel = lambda text, channel=None, metadata=None, reaktionen=(): "1.0"
    slack_utils.send_dm = lambda m, text, metadata=None, reaktionen=(): "123.456"

    try:
        members = [member(f"N{i}", new=True) for i in range(10)] + [member(f"A{i}") for i in range(10)]
        members.append(member("KUERZLICH", putz_pages=["w50"]))
        lookup = {m["id"]: m for m in members}
        lookup["FREIWILLIG"] = member("FREIWILLIG")

        week_pages = {
            "by_week": {
                (1, 2027): {"page_id": "p1", "page_url": "u1", "kw": 1, "year": 2027,
                            "member_ids": ["FREIWILLIG"], "member_count": 1,
                            "status": "Geplant", "archiv": False},
                (3, 2027): {"page_id": "p3", "page_url": "u3", "kw": 3, "year": 2027,
                            "member_ids": [], "member_count": 0,
                            "status": config.WEEK_STATUS_BLOCKED, "archiv": False},
            },
            "by_page_id": {
                "w50": {"page_id": "w50", "kw": 50, "year": 2026, "member_ids": ["KUERZLICH"],
                        "member_count": 1, "status": "Erledigt", "archiv": False, "page_url": ""},
            },
        }

        summary = scheduler.plan_next_cycle(week_pages, members, lookup, 53, 2026)
        planned = {kw: [m["id"] for m in crew] for kw, crew, _ in summary}

        check("gesperrte Woche wird übersprungen", 3 in planned, False)
        check("drei Wochen geplant", sorted(planned), [1, 2, 4])
        check("jede Woche voll besetzt", {kw: len(ids) for kw, ids in planned.items()}, {1: 4, 2: 4, 4: 4})
        check("Freiwilliger bleibt eingetragen", planned[1][0], "FREIWILLIG")

        assigned = [mid for ids in planned.values() for mid in ids]
        check("niemand doppelt im Zyklus", len(assigned), len(set(assigned)))
        check("Mitglied in Schonfrist bleibt außen vor", "KUERZLICH" in assigned, False)

        for kw, ids in planned.items():
            neu = sum(1 for mid in ids if lookup[mid]["ist_neu"])
            check(f"KW {kw}: 2 neue + 2 alte", neu, 2)

        check("bestehende Seite wird geupdated", ("update", "p1", 4) in writes, True)
        check("fehlende Seiten werden angelegt", sum(1 for w in writes if w[0] == "create"), 2)
        check("volle Crew bekommt Status", all(w[-1] == config.WEEK_STATUS_FULL for w in writes if w[0] in ("create", "status")), True)
    finally:
        (
            notion.update_page_members,
            notion.set_week_status,
            notion.create_week_page,
            slack_utils.get_slack_user_id,
            slack_utils.post_channel,
            slack_utils.send_dm,
        ) = original


def test_draw_flow():
    print("\n=== Draw-Lauf (Übergangsmodus: eine Woche, keine DMs) ===")

    writes, dms, posts = [], [], []
    restore = _fake_slack_notion(writes, dms, {})
    # _fake_slack_notion verschluckt die Kanalnachricht — hier wollen wir sie sehen.
    slack_utils.post_channel = lambda text, channel=None, metadata=None, reaktionen=(): (
        posts.append((text, metadata, tuple(reaktionen))) or "1.0"
    )

    try:
        members = [member(f"N{i}", new=True) for i in range(6)] + [member(f"A{i}") for i in range(6)]
        lookup = {m["id"]: m for m in members}
        lookup["FREIWILLIG"] = member("FREIWILLIG")

        kw32 = {"page_id": "p32", "page_url": "u32", "kw": 32, "year": 2026,
                "member_ids": ["FREIWILLIG"], "member_count": 1,
                "status": "Geplant", "archiv": False}
        week_pages = {"by_week": {(32, 2026): kw32}, "by_page_id": {"p32": kw32}}

        selected = scheduler.draw_current_week(week_pages, members, lookup, 32, 2026)

        check("drei Plätze aufgefüllt", len(selected), 3)
        check("Freiwilliger wurde nicht erneut gezogen",
              "FREIWILLIG" in [m["id"] for m in selected], False)
        check("Woche wurde geschrieben",
              [w for w in writes if w[0] == "update" and w[1] == "p32"] != [], True)
        check("Woche ist jetzt voll", week_pages["by_week"][(32, 2026)]["member_count"], 4)

        check("KEINE DMs verschickt", dms, [])
        check("genau eine Kanalnachricht", len(posts), 1)
        text, metadata, reaktionen = posts[0]
        check("Nachricht nennt die KW", "KW 32" in text, True)
        check("Freiwillige werden getrennt gedankt", "freiwillige" in text.lower(), True)
        check("Ausgeloste werden genannt", "Ausgelost" in text, True)
        check("Link zur Seite dabei", "u32" in text, True)
        check("Besen-Bestaetigung wird erklaert", f":{config.PUTZ_REACTION}:" in text, True)
        check("Metadata traegt KW und Jahr",
              (metadata["event_type"], metadata["event_payload"]),
              (config.META_WOCHE, {"kw": 32, "jahr": 2026}))
        check("Besen ist vorgesetzt", reaktionen, (config.PUTZ_REACTION,))

        # KW 33 ist die erste Woche des Folgezyklus — der darf NICHT mitgeplant
        # werden, auch wenn KW 32 die letzte Woche von Zyklus 8 ist.
        check("kein Zyklus-Plan-Lauf nebenbei",
              [w for w in writes if w[0] == "create"], [])

        print("\n=== Draw-Lauf: gesperrte Woche ===")
        posts.clear()
        writes.clear()
        # Nicht kw32 anfassen: fill_week hat den Cache-Eintrag durch ein neues
        # Dict ersetzt, das alte Objekt hängt nirgends mehr.
        week_pages["by_week"][(32, 2026)]["status"] = config.WEEK_STATUS_BLOCKED
        selected = scheduler.draw_current_week(week_pages, members, lookup, 32, 2026)
        check("nichts ausgelost", selected, [])
        check("nichts geschrieben", writes, [])
        check("keine Kanalnachricht", posts, [])
    finally:
        restore()


def bot_msg(event_type=None, payload=None, reaktionen=(), ts="1", text=""):
    return {"ts": ts, "text": text, "ist_vom_bot": True, "event_type": event_type,
            "payload": payload or {}, "reaktionen": set(reaktionen)}


def user_msg(text, ts="2"):
    return {"ts": ts, "text": text, "ist_vom_bot": False, "event_type": None,
            "payload": {}, "reaktionen": set()}


def test_reschedule_logik():
    print("\n=== Reschedule: Eingaben parsen ===")
    check("nackte Zahl", reschedule.parse_wochennummer("22"), 22)
    check("mit Präfix", reschedule.parse_wochennummer("KW 22"), 22)
    check("im Satz", reschedule.parse_wochennummer("ich würde gerne in 22 putzen"), 22)
    check("ohne Zahl", reschedule.parse_wochennummer("keine Ahnung"), None)
    check("unmögliche KW", reschedule.parse_wochennummer("99"), None)
    check("leer", reschedule.parse_wochennummer(""), None)

    print("\n=== Reschedule: Antwort zitieren ===")
    check("kurze Antwort bleibt", reschedule.kurzfassung("22"), "22")
    check("Zeilenumbrueche werden zusammengezogen",
          reschedule.kurzfassung("KW\n22"), "KW 22")
    check("Backticks fliegen raus (sonst bricht der Code-Span)",
          reschedule.kurzfassung("`22`"), "'22'")
    lang = reschedule.kurzfassung("ich weiss es wirklich nicht so genau ehrlich gesagt")
    check("lange Antwort wird gekuerzt", len(lang) <= 41, True)
    check("... mit Auslassung statt hartem Schnitt", lang.endswith("…"), True)
    check("... und nicht mitten im Wort", lang.rstrip("…").endswith(" "), False)

    print("\n=== Reschedule: Zielwoche bestimmen ===")
    # Stand KW 31/2026 (2026 hat 53 Wochen)
    check("spätere KW -> selbes Jahr",
          reschedule.zielwoche_bestimmen(35, 31, 2026)[0], (35, 2026))
    check("frühere KW -> Folgejahr",
          reschedule.zielwoche_bestimmen(5, 31, 2026)[0], (5, 2027))
    check("KW 53 in 53-Wochen-Jahr geht",
          reschedule.zielwoche_bestimmen(53, 31, 2026)[0], (53, 2026))
    # Stand KW 31/2027 (2027 hat nur 52) -> KW 53 gäbe es erst 2028, das hat auch keine
    check("KW 53 in 52-Wochen-Jahr wird abgelehnt",
          reschedule.zielwoche_bestimmen(53, 31, 2027)[0], None)
    # Jahreswechsel, der tatsächlich in Reichweite liegt: KW 50/2026 -> KW 5/2027
    check("Jahreswechsel in Reichweite",
          reschedule.zielwoche_bestimmen(5, 50, 2026)[0], (5, 2027))
    # Dieselbe KW landet im Folgejahr und ist damit ~52 Wochen weg -> zu weit
    check("dieselbe KW ist zu weit weg",
          reschedule.zielwoche_bestimmen(31, 31, 2026)[0], None)
    check("... mit passender Begründung",
          "Zyklen" in reschedule.zielwoche_bestimmen(31, 31, 2026)[1], True)
    check("zu weit weg wird abgelehnt",
          reschedule.zielwoche_bestimmen(30, 31, 2026)[0], None)
    check("Vergangenheit wird abgelehnt",
          reschedule.zielwoche_bestimmen(31, 31, 2026)[0], None)
    check("Unsinn wird abgelehnt", reschedule.zielwoche_bestimmen(None, 31, 2026)[0], None)

    print("\n=== Reschedule: Kapazität (harte Grenze bei 4) ===")
    check("leere Woche", reschedule.ist_platz_frei(0), True)
    check("3 Leute -> passt noch jemand", reschedule.ist_platz_frei(3), True)
    check("4 Leute -> voll", reschedule.ist_platz_frei(4), False)
    check("5 Leute -> voll", reschedule.ist_platz_frei(5), False)

    print("\n=== Reschedule: Zustand aus dem DM-Verlauf ===")
    aktuell = {(5, 2027)}
    auslosung = bot_msg(config.META_AUSLOSUNG, {"kw": 5, "jahr": 2027}, ["x"])

    check("❌ auf Auslosung -> Absage",
          reschedule.naechster_zustand([auslosung], aktuell), ("absage", (5, 2027)))
    check("✅ auf Auslosung -> nichts",
          reschedule.naechster_zustand(
              [bot_msg(config.META_AUSLOSUNG, {"kw": 5, "jahr": 2027}, ["white_check_mark"])],
              aktuell)[0], None)
    check("keine Reaktion -> nichts",
          reschedule.naechster_zustand(
              [bot_msg(config.META_AUSLOSUNG, {"kw": 5, "jahr": 2027})], aktuell)[0], None)
    check("❌ zu einer Woche, in der man nicht mehr steht -> nichts",
          reschedule.naechster_zustand([auslosung], {(9, 2027)})[0], None)

    frage = bot_msg(config.META_FRAGE, {"kw": 5, "jahr": 2027}, ts="10")
    check("Frage ohne Antwort -> nichts",
          reschedule.naechster_zustand([frage, auslosung], aktuell)[0], None)
    check("Frage mit Antwort -> Antwort",
          reschedule.naechster_zustand([user_msg("22"), frage, auslosung], aktuell),
          ("antwort", ("22", (5, 2027))))

    # Der Anker: haben wir schon gefragt, wird dieselbe Absage nicht nochmal bearbeitet
    check("nach dem Nachfragen greift die alte ❌ nicht erneut",
          reschedule.naechster_zustand([frage, auslosung], aktuell)[0], None)
    # Und nach unserer Bestätigung ist ebenfalls Ruhe
    check("gewöhnliche Bot-Nachricht danach stoppt die Verarbeitung",
          reschedule.naechster_zustand(
              [bot_msg(None, None, [], ts="11"), user_msg("22"), frage, auslosung],
              aktuell)[0], None)
    check("Bestätigung mit Metadata stoppt ebenfalls",
          reschedule.naechster_zustand(
              [bot_msg(config.META_BESTAETIGUNG, {"kw": 22, "jahr": 2027, "mitglied": "M0"},
                       ts="11"),
               user_msg("22"), frage, auslosung],
              aktuell)[0], None)

    print("\n=== Reschedule: DMs dem richtigen Mitglied zuordnen ===")
    # Mit SLACK_TEST_USER_ID landen alle DMs im selben Kanal. Ohne Filter wäre
    # die neueste Nachricht dort die eines anderen Mitglieds.
    meins = bot_msg(config.META_AUSLOSUNG, {"kw": 5, "jahr": 2027, "mitglied": "M0"},
                    ["x"], ts="1")
    fremd = bot_msg(config.META_AUSLOSUNG, {"kw": 5, "jahr": 2027, "mitglied": "M9"},
                    ["x"], ts="2")

    check("fremde Bot-Nachricht wird aussortiert",
          reschedule.verlauf_fuer([fremd, meins], "M0"), [meins])
    check("Nachricht ohne Zuordnung bleibt (alter Bestand)",
          reschedule.verlauf_fuer([bot_msg(None, None, [], ts="3"), meins], "M0") != [meins],
          True)
    check("Antworten des Mitglieds bleiben immer drin",
          reschedule.verlauf_fuer([user_msg("22"), fremd], "M0"), [user_msg("22")])

    check("fremde ❌ löst für mich nichts aus",
          reschedule.naechster_zustand(
              reschedule.verlauf_fuer([fremd], "M0"), aktuell)[0], None)
    check("meine eigene ❌ greift weiterhin",
          reschedule.naechster_zustand(
              reschedule.verlauf_fuer([fremd, meins], "M0"), aktuell), ("absage", (5, 2027)))


def _fake_slack_notion(writes, dms, verlauf_pro_mitglied):
    """Notion- und Slack-Aufrufe durch Rekorder ersetzen. Gibt ein Restore-Callable zurück."""
    original = {
        "update": notion.update_page_members,
        "status": notion.set_week_status,
        "create": notion.create_week_page,
        "uid": slack_utils.get_slack_user_id,
        "post": slack_utils.post_channel,
        "dm": slack_utils.send_dm,
        "hist": slack_utils.read_dm_history,
    }
    notion.update_page_members = lambda pid, ids: writes.append(("update", pid, list(ids))) or True
    notion.set_week_status = lambda pid, s: writes.append(("status", pid, s)) or True
    notion.create_week_page = lambda kw, jahr, ids, status="Geplant": (
        writes.append(("create", kw, jahr, list(ids), status))
        or (f"page-{kw}-{jahr}", f"https://notion.so/kw{kw}")
    )
    slack_utils.get_slack_user_id = lambda email: "U1"
    slack_utils.post_channel = lambda text, channel=None, metadata=None, reaktionen=(): "1.0"
    slack_utils.send_dm = lambda m, text, metadata=None, reaktionen=(): (
        dms.append((m["id"], (metadata or {}).get("event_type"), text)) or "9.9"
    )
    slack_utils.read_dm_history = lambda m: verlauf_pro_mitglied.get(m["id"], [])

    def restore():
        notion.update_page_members = original["update"]
        notion.set_week_status = original["status"]
        notion.create_week_page = original["create"]
        slack_utils.get_slack_user_id = original["uid"]
        slack_utils.post_channel = original["post"]
        slack_utils.send_dm = original["dm"]
        slack_utils.read_dm_history = original["hist"]

    return restore


def _poll_szenario(verlauf, ziel_woche_belegung=None):
    """Baut eine Welt mit KW 35/2026 (4 Leute) und laesst run_poll darueber laufen."""
    members = [member(f"M{i}", new=(i % 2 == 0)) for i in range(12)]
    lookup = {m["id"]: m for m in members}

    kw35 = {"page_id": "p35", "page_url": "u35", "kw": 35, "year": 2026,
            "member_ids": ["M0", "M1", "M2", "M3"], "member_count": 4,
            "status": "Crew voll", "archiv": False}
    week_pages = {"by_week": {(35, 2026): kw35}, "by_page_id": {"p35": kw35}}

    if ziel_woche_belegung is not None:
        kw40 = {"page_id": "p40", "page_url": "u40", "kw": 40, "year": 2026,
                "member_ids": list(ziel_woche_belegung),
                "member_count": len(ziel_woche_belegung),
                "status": "Geplant", "archiv": False}
        week_pages["by_week"][(40, 2026)] = kw40
        week_pages["by_page_id"]["p40"] = kw40

    writes, dms = [], []
    restore = _fake_slack_notion(writes, dms, {"M0": verlauf})
    try:
        reschedule.run_poll(week_pages, members, lookup, 31, 2026)
    finally:
        restore()
    return writes, dms, week_pages


def test_poll_flow():
    print("\n=== Poll: Absage wird zur Nachfrage ===")
    auslosung = bot_msg(config.META_AUSLOSUNG, {"kw": 35, "jahr": 2026}, ["x"])
    writes, dms, _ = _poll_szenario([auslosung])
    check("keine Notion-Schreibzugriffe", writes, [])
    check("genau eine DM", len(dms), 1)
    check("DM ist die Nachfrage", dms[0][1], config.META_FRAGE)

    print("\n=== Poll: Antwort traegt um ===")
    frage = bot_msg(config.META_FRAGE, {"kw": 35, "jahr": 2026}, ts="10")
    writes, dms, week_pages = _poll_szenario([user_msg("40"), frage, auslosung])

    updates = [w for w in writes if w[0] == "update" and w[1] == "p35"]
    check("alte Woche wurde geschrieben", len(updates) >= 1, True)
    check("M0 ist aus KW 35 raus", "M0" in updates[0][2], False)
    creates = [w for w in writes if w[0] == "create"]
    check("Zielwoche wurde angelegt", len(creates), 1)
    check("... als KW 40/2026", (creates[0][1], creates[0][2]), (40, 2026))
    check("... mit M0 drin", creates[0][3], ["M0"])
    check("Bestaetigung an M0",
          any(d[0] == "M0" and d[1] == config.META_BESTAETIGUNG for d in dms), True)

    # Nach dem Austragen ist KW 35 unterbesetzt -> es muss nachgelost werden
    nachgelost = [d for d in dms if d[1] == config.META_AUSLOSUNG]
    check("es wurde nachgelost", len(nachgelost), 1)
    check("M0 wurde nicht erneut gezogen", nachgelost[0][0] == "M0", False)
    check("KW 35 ist wieder voll",
          week_pages["by_week"][(35, 2026)]["member_count"], 4)

    print("\n=== Poll: volle Zielwoche wird abgelehnt ===")
    writes, dms, _ = _poll_szenario([user_msg("40"), frage, auslosung],
                                    ziel_woche_belegung=["M5", "M6", "M7", "M9"])
    check("nichts umgetragen", [w for w in writes if w[0] == "update"], [])
    check("erneute Nachfrage", dms[0][1], config.META_FRAGE)
    check("Begruendung nennt die Belegung", "4" in dms[0][2], True)
    check("Link zur Woche dabei", "u40" in dms[0][2], True)
    check("die dortige Crew wird NICHT behelligt",
          [d for d in dms if d[0] != "M0"], [])

    print("\n=== Poll: teilbesetzte Zielwoche wird angenommen ===")
    writes, dms, _ = _poll_szenario([user_msg("40"), frage, auslosung],
                                    ziel_woche_belegung=["M5", "M6", "M7"])
    ziel_updates = [w for w in writes if w[0] == "update" and w[1] == "p40"]
    check("Zielwoche wurde geschrieben", len(ziel_updates), 1)
    check("M0 ist drin", "M0" in ziel_updates[0][2], True)
    check("Zielwoche ist jetzt genau voll", len(ziel_updates[0][2]), 4)

    print("\n=== Poll: Unsinn fuehrt zu erneuter Nachfrage ===")
    writes, dms, _ = _poll_szenario([user_msg("weiss nicht"), frage, auslosung])
    check("nichts umgetragen", [w for w in writes if w[0] == "update"], [])
    check("erneute Nachfrage", dms[0][1], config.META_FRAGE)


def test_eigene_reaktionen_filtern():
    """Der Kern des Vorsetzens: eigene Reaktionen dürfen nicht als Antwort zählen.

    Ohne diese Trennung stünde auf JEDER Auslos-DM ein ❌ vom Bot selbst, und der
    nächste Poll würde die komplette Crew nach einer Wunschwoche fragen.
    """
    print("\n=== Vorgesetzte Reaktionen von echten trennen ===")

    BOT, MITGLIED = "UBOT", "UMEMBER"

    def verlauf_mit(reactions):
        original_hist = slack_utils.slack.conversations_history
        original_dm = slack_utils.dm_channel
        original_id = slack_utils._eigene_id.get("id", "__nicht_gesetzt__")
        slack_utils.dm_channel = lambda m: "D1"
        slack_utils.slack.conversations_history = lambda **kw: {
            "messages": [{"ts": "1", "text": "Auslosung", "bot_id": "B1",
                          "reactions": reactions}]
        }
        slack_utils._eigene_id["id"] = BOT
        try:
            return slack_utils.read_dm_history({"name": "Test", "email": "t@x.de"})
        finally:
            slack_utils.slack.conversations_history = original_hist
            slack_utils.dm_channel = original_dm
            if original_id == "__nicht_gesetzt__":
                slack_utils._eigene_id.pop("id", None)
            else:
                slack_utils._eigene_id["id"] = original_id

    nur_bot = verlauf_mit([{"name": "x", "users": [BOT], "count": 1},
                           {"name": "white_check_mark", "users": [BOT], "count": 1}])
    check("nur vorgesetzt -> keine Reaktion", nur_bot[0]["reaktionen"], set())
    check("... und damit keine Absage", slack_utils.reaktion_auf(nur_bot[0]), None)

    geklickt = verlauf_mit([{"name": "x", "users": [BOT, MITGLIED], "count": 2},
                            {"name": "white_check_mark", "users": [BOT], "count": 1}])
    check("Mitglied klickt ❌ -> Absage", slack_utils.reaktion_auf(geklickt[0]), "nein")

    zugesagt = verlauf_mit([{"name": "x", "users": [BOT], "count": 1},
                            {"name": "white_check_mark", "users": [BOT, MITGLIED], "count": 2}])
    check("Mitglied klickt ✅ -> Zusage", slack_utils.reaktion_auf(zugesagt[0]), "ja")

    beides = verlauf_mit([{"name": "x", "users": [BOT, MITGLIED], "count": 2},
                          {"name": "white_check_mark", "users": [BOT, MITGLIED], "count": 2}])
    check("beides geklickt -> Absage gewinnt", slack_utils.reaktion_auf(beides[0]), "nein")

    fremd = verlauf_mit([{"name": "tada", "users": [MITGLIED], "count": 1}])
    check("unbekanntes Emoji bleibt folgenlos", slack_utils.reaktion_auf(fremd[0]), None)


def test_reaktionen_vorsetzen():
    """Nur die Auslos-DM bekommt Emojis vorgesetzt — die Nachfrage nicht.

    Auf die Nachfrage wird eine Zahl als Text erwartet; vorgesetzte Emojis wären
    dort eine falsche Fährte.
    """
    print("\n=== Vorsetzen nur auf der Auslos-DM ===")

    dms, writes = [], []
    restore = _fake_slack_notion(writes, dms, {})
    gesendet = []
    slack_utils.send_dm = lambda m, text, metadata=None, reaktionen=(): (
        gesendet.append(((metadata or {}).get("event_type"), tuple(reaktionen))) or "9.9"
    )
    try:
        members = [member(f"M{i}") for i in range(6)]
        lookup = {m["id"]: m for m in members}
        woche = {"page_id": "p", "page_url": "u", "kw": 40, "year": 2026,
                 "member_ids": [], "member_count": 0, "status": "Geplant",
                 "archiv": False, "page_status": "exists"}
        week_pages = {"by_week": {(40, 2026): woche}, "by_page_id": {"p": woche}}

        scheduler.fill_week(dict(woche), members, week_pages, lookup)
        check("Auslos-DMs verschickt", len(gesendet), 4)
        check("... alle mit beiden Emojis vorbelegt",
              all(r == config.PREFILL_REACTIONS for _, r in gesendet), True)
        check("... und die Reihenfolge passt zum Text (✅ vor ❌)",
              config.PREFILL_REACTIONS, ("white_check_mark", "x"))

        gesendet.clear()
        scheduler.fill_week(dict(woche, member_ids=[], member_count=0),
                            members, week_pages, lookup, send_dms=False)
        check("draw-Modus setzt nichts vor", gesendet, [])
    finally:
        restore()


def test_post_channel_schalter():
    """Die ECHTE post_channel einmal ausführen.

    Alle anderen Tests ersetzen sie durch ein Lambda — dadurch ist ein
    fehlender Import in ihr nie aufgefallen. Genau das ist passiert: DM_ONLY
    stand in config, war aber in slack_utils nicht importiert, und der
    NameError fiel erst im Produktivlauf auf. Hier läuft die Funktion selbst.
    """
    print("\n=== post_channel: Schalter greifen ===")

    check("DM_ONLY ist in slack_utils bekannt", hasattr(slack_utils, "DM_ONLY"), True)
    check("DRY_RUN ist in slack_utils bekannt", hasattr(slack_utils, "DRY_RUN"), True)

    original_dry, original_dm = slack_utils.DRY_RUN, slack_utils.DM_ONLY
    try:
        # Der Rückgabewert ist inzwischen der Message-Timestamp, taugt also nicht
        # mehr als Beweis. Stattdessen wird der Versand selbst beobachtet: kommt
        # die Funktion bis dorthin, ist der Schalter wirkungslos.
        versuche = []
        original_post = slack_utils.slack.chat_postMessage
        slack_utils.slack.chat_postMessage = lambda **kw: versuche.append(kw)
        try:
            slack_utils.DRY_RUN, slack_utils.DM_ONLY = True, False
            slack_utils.post_channel("Test")
            check("DRY_RUN unterdrückt den Versand", versuche, [])

            slack_utils.DRY_RUN, slack_utils.DM_ONLY = False, True
            slack_utils.post_channel("Test")
            check("DM_ONLY unterdrückt den Versand", versuche, [])
        finally:
            slack_utils.slack.chat_postMessage = original_post
    finally:
        slack_utils.DRY_RUN, slack_utils.DM_ONLY = original_dry, original_dm


def test_filter_umfang():
    print("\n=== Filter: Tag-Pruefung greift weiter als der Lostopf ===")

    def bedingungen(f):
        """Alle 'property'-Namen aus einem verschachtelten Notion-Filter."""
        namen = set()
        if isinstance(f, dict):
            if "property" in f:
                namen.add(f["property"])
            for wert in f.values():
                namen |= bedingungen(wert)
        elif isinstance(f, list):
            for eintrag in f:
                namen |= bedingungen(eintrag)
        return namen

    lostopf = bedingungen(notion.MEMBER_FILTER)
    tags = bedingungen(notion.TAG_CHECK_FILTER)

    # Beide grenzen auf aktive Mitglieder ein — das ist gewollt deckungsgleich.
    for prop in ("Austrittsdatum", "Onboarding: Status", "Mitgliedsstatus", "Putzstatus"):
        check(f"beide filtern auf {prop}", prop in lostopf and prop in tags, True)

    # Der Unterschied liegt allein im Putzstatus, und darauf kommt es an:
    # der Lostopf laesst NUR die losbaren Werte zu, die Tag-Pruefung schliesst
    # nur 'Ausgetragen' aus. Wer heute 'Neu' ist, ist in zwei Monaten 'Normal' —
    # dann soll die E-Mail schon geprueft sein und nicht erst beim ersten Einsatz
    # auffallen.
    putz_lostopf = {w for p, w in notion._filter_auswahlwerte(notion.MEMBER_FILTER)
                    if p == "Putzstatus"}
    putz_tags = {w for p, w in notion._filter_auswahlwerte(notion.TAG_CHECK_FILTER)
                 if p == "Putzstatus"}

    check("Lostopf laesst nur die losbaren Werte zu",
          putz_lostopf, set(config.PUTZSTATUS_ELIGIBLE))
    check("leerer Putzstatus ist NICHT losbar", None in config.PUTZSTATUS_ELIGIBLE, False)
    check("Tag-Pruefung nennt nur 'Ausgetragen'", putz_tags, {"Ausgetragen"})
    check("... und prueft damit auch 'Neu' und 'Postponed'",
          putz_tags & {"Neu", "Postponed", "Priorität"}, set())

    print("\n=== Filter: Auswahlwerte gegen das Notion-Schema ===")
    werte = notion._filter_auswahlwerte(notion.MEMBER_FILTER)
    mitglied = {w for p, w in werte if p == "Mitgliedsstatus"}

    # In der Produktion aufgefallen: Notion-Optionen wurden umbenannt
    # ('Vorläufiges Mitglied' -> 'Probemitglied', 'passives Mitglied' -> 'passiv').
    # Der Filter traf danach lautlos 34 Mitglieder weniger.
    check("Werte werden aus dem Filter gelesen",
          {"Vereinsmitglied", "Probemitglied", "Probemitglied (+1 Jahr)"} <= mitglied, True)
    check("alte Namen sind raus",
          any("Vorläufiges" in w or w == "passives Mitglied" for w in mitglied), False)
    check("passiv wird ausgeschlossen", "passiv" in mitglied, True)
    check("gekündigt wird ausgeschlossen", "gekündigt" in mitglied, True)
    check("Putzstatus-Werte werden mitgelesen",
          ("Putzstatus", "Normal") in werte, True)

    # Ausschliessende Operatoren muessen genauso mitgelesen werden wie
    # einschliessende: TAG_CHECK_FILTER haengt 'Ausgetragen' an does_not_equal,
    # und ein Ausschluss auf eine umbenannte Option ist genauso still kaputt —
    # nur andersherum, dann rutschen ploetzlich Leute rein.
    check("does_not_equal wird mitgelesen",
          ("Putzstatus", "Ausgetragen")
          in notion._filter_auswahlwerte(notion.TAG_CHECK_FILTER), True)
    check("alle vier Operatoren sind abgedeckt",
          set(notion.AUSWAHL_OPERATOREN),
          {"equals", "does_not_equal", "contains", "does_not_contain"})

    # Fehlende Optionen müssen auffallen — hier gegen ein gefaktes Schema.
    echtes_schema = {
        "Mitgliedsstatus": {"multi_select": {"options": [
            {"name": n} for n in ("Vereinsmitglied", "Probemitglied",
                                  "Probemitglied (+1 Jahr)", "Jugendliches Mitglied",
                                  "Fördermitglied", "passiv", "gekündigt")]}},
        "Putzstatus": {"select": {"options": [{"name": n} for n in ("Normal", "Neu")]}},
        "Onboarding: Status": {"select": {"options": [{"name": "Erledigt"}]}},
    }
    for prop in echtes_schema:
        echtes_schema[prop]["type"] = "multi_select" if prop == "Mitgliedsstatus" else "select"

    def pruefe_gegen(schema):
        vorhanden = {
            name: {o["name"] for o in p[p["type"]]["options"]}
            for name, p in schema.items()
        }
        return [(prop, wert) for prop, wert in sorted(werte)
                if prop in vorhanden and wert not in vorhanden[prop]]

    check("aktuelles Schema passt zum Filter", pruefe_gegen(echtes_schema), [])

    umbenannt = dict(echtes_schema)
    umbenannt["Mitgliedsstatus"] = {
        "type": "multi_select",
        "multi_select": {"options": [{"name": "Vereinsmitglied"}, {"name": "Neuer Name"}]},
    }
    check("Umbenennung wird erkannt", len(pruefe_gegen(umbenannt)) > 0, True)


def test_clean_string():
    print("\n=== Abgeleitete E-Mail-Adressen ===")
    check("Umlaute", notion.clean_string("Müller"), "mueller")
    check("Diakritika", notion.clean_string("Colatat"), "colatat")
    check("Bindestriche bleiben", notion.clean_string("Heinrich-Ziegler"),
          "heinrich-ziegler")
    # In der Produktion aufgefallen: 'remco.van de ven@das-habitat.de'
    check("mehrteiliger Nachname ohne Leerzeichen",
          notion.clean_string("van de Ven"), "vandeven")
    check("führende/anhängende Leerzeichen", notion.clean_string("  Kaufhold "),
          "kaufhold")
    # Ebenfalls in der Produktion aufgefallen: 'jacqueline(jacky).hoeger@…'
    check("Spitzname in Klammern fliegt raus",
          notion.clean_string("Jacqueline (Jacky)"), "jacqueline")
    check("... auch mitten im Namen",
          notion.clean_string("Anna (Anni) Maria"), "annamaria")


def test_tagcheck():
    print("\n=== Tag-Pruefung ===")

    # (email -> user_id) bzw. Fehlercode; None heisst 'nicht gefunden'
    antworten = {
        "gut@das-habitat.de": "U_GUT",
        "geraten@das-habitat.de": "U_GERATEN",
    }
    aufrufe = []

    def fake_lookup(email):
        aufrufe.append(email)
        if email in antworten:
            return antworten[email], None
        return None, "users_not_found"

    original = tagcheck._lookup
    tagcheck._lookup = fake_lookup
    try:
        mitglieder = [
            {"id": "1", "name": "Gut, Greta", "email": "gut@das-habitat.de",
             "email_quelle": "Interne Email"},
            {"id": "2", "name": "Geraten, Gerd", "email": "geraten@das-habitat.de",
             "email_quelle": "abgeleitet"},
            {"id": "3", "name": "Weg, Willi", "email": "weg@das-habitat.de",
             "email_quelle": "abgeleitet"},
            {"id": "4", "name": "Zadlo, ", "email": None, "email_quelle": None},
        ]

        erreichbar, fehlend = tagcheck.pruefe(mitglieder)

        check("zwei erreichbar", [m["id"] for m, _ in erreichbar], ["1", "2"])
        check("zwei nicht erreichbar", [m["id"] for m, _ in fehlend], ["3", "4"])
        check("ohne E-Mail wird gar nicht erst nachgeschlagen",
              "weg@das-habitat.de" in aufrufe and len(aufrufe), 3)
        check("Grund bei fehlendem Slack-User", fehlend[0][1], "users_not_found")
        check("Grund bei fehlender E-Mail", "keine E-Mail" in fehlend[1][1], True)

        bericht = tagcheck.baue_bericht(erreichbar, fehlend)
        check("Bericht sagt, dass er weiter greift als der Lostopf",
              "mehr als der heutige Lostopf" in bericht, True)
        check("Bericht nennt beide Zahlen",
              "✅ 2 erreichbar" in bericht and "❌ 2 nicht erreichbar" in bericht, True)
        check("nicht Erreichbare stehen mit Namen drin", "Zadlo" in bericht, True)
        check("Erreichbare stehen als Erwähnung drin", "<@U_GUT>" in bericht, True)
        check("geratene Adressen werden hervorgehoben",
              "geraten" in bericht.lower() and "Geraten, Gerd" in bericht, True)

        print("\n=== Tag-Pruefung: alles sauber ===")
        bericht = tagcheck.baue_bericht([(mitglieder[0], "U_GUT")], [])
        check("ohne Fehlende keine Mängelliste",
              "keine DM" in bericht, False)
        check("... aber die Erreichbaren trotzdem", "<@U_GUT>" in bericht, True)
    finally:
        tagcheck._lookup = original


# ------------------------------------------------------------- Erledigt-Tracking

def _tracking_welt(woche_kw, member_ids, reagierende, verlaeufe, nachricht=True,
                   status="Crew voll"):
    """Welt fuer den Erledigt-Abgleich. Gibt (writes, dms, week_pages, lookup) zurueck."""
    members = [member(f"M{i}") for i in range(8)]
    lookup = {m["id"]: m for m in members}

    woche = {"page_id": f"p{woche_kw}", "page_url": f"u{woche_kw}", "kw": woche_kw,
             "year": 2026, "member_ids": list(member_ids),
             "member_count": len(member_ids), "status": status, "archiv": False}
    week_pages = {"by_week": {(woche_kw, 2026): woche},
                  "by_page_id": {woche["page_id"]: woche}}

    writes, dms = [], []
    restore = _fake_slack_notion(writes, dms, verlaeufe)
    original = (slack_utils.finde_wochennachrichten, slack_utils.reagierende_user,
                slack_utils.slack_id_fuer, slack_utils.email_fuer_user)
    slack_utils.finde_wochennachrichten = lambda oldest=None, channel=None: (
        {(woche_kw, 2026): "111.1"} if nachricht else {}
    )
    slack_utils.reagierende_user = lambda ts, emoji, channel=None: set(reagierende)
    slack_utils.slack_id_fuer = lambda m: f"U{m['id']}"
    slack_utils.email_fuer_user = lambda uid: f"{uid[1:]}@das-habitat.de"
    return writes, dms, week_pages, lookup, restore, original


def _tracking_lauf(heute_kw, **kwargs):
    writes, dms, week_pages, lookup, restore, original = _tracking_welt(**kwargs)
    try:
        tracking.run_abgleich(week_pages, lookup, heute_kw, 2026)
    finally:
        (slack_utils.finde_wochennachrichten, slack_utils.reagierende_user,
         slack_utils.slack_id_fuer, slack_utils.email_fuer_user) = original
        restore()
    return writes, dms, week_pages


def test_tracking_fenster():
    print("\n=== Erledigt: welche Wochen werden ueberhaupt angefasst ===")

    def woche(kw, jahr=2026, status="Crew voll", archiv=False):
        return {"page_id": f"p{kw}", "page_url": "", "kw": kw, "year": jahr,
                "member_ids": [], "member_count": 0, "status": status, "archiv": archiv}

    week_pages = {"by_week": {}, "by_page_id": {}}
    for eintrag in [
        woche(30),                                  # vor TRACKING_START
        woche(36),                                  # vor TRACKING_START
        woche(37),                                  # zu weit zurueck (Lookback)
        woche(42),                                  # dran
        woche(43),                                  # dran
        woche(44),                                  # laufende Woche
        woche(45),                                  # Zukunft
        woche(41, status=config.WEEK_STATUS_DONE),  # schon abgeschlossen
        woche(40, status=config.WEEK_STATUS_BLOCKED),
        woche(39, archiv=True),
    ]:
        week_pages["by_week"][(eintrag["kw"], eintrag["year"])] = eintrag

    offen = [w["kw"] for w, _ in tracking.offene_wochen(week_pages, 44, 2026)]
    check("nur die offenen Vergangenheitswochen", offen, [42, 43])
    check("Reihenfolge: aelteste zuerst", offen, sorted(offen))

    check("Start-Grenze haengt an der Konstante",
          (config.TRACKING_START_KW, config.TRACKING_START_YEAR), (37, 2026))


def test_tracking_abgleich():
    print("\n=== Erledigt: Abgleich fragt die Stillen ===")
    # KW 43 liegt eine Woche zurueck -> Nachfragen, aber noch kein Abschluss.
    writes, dms, week_pages = _tracking_lauf(
        44, woche_kw=43, member_ids=["M0", "M1", "M2", "M3"],
        reagierende={"UM0", "UM1"}, verlaeufe={})

    check("nichts in Notion geschrieben", writes, [])
    check("zwei Nachfragen", len(dms), 2)
    check("... an die Stillen", sorted(d[0] for d in dms), ["M2", "M3"])
    check("... mit Erledigt-Metadata",
          {d[1] for d in dms}, {config.META_ERLEDIGT_FRAGE})
    check("Woche bleibt offen", week_pages["by_week"][(43, 2026)]["status"], "Crew voll")

    print("\n=== Erledigt: Abschluss traegt die Stillen aus ===")
    gefragt = {
        "M2": [bot_msg(config.META_ERLEDIGT_FRAGE,
                       {"kw": 42, "jahr": 2026, "mitglied": "M2", "art": "ausgelost"})],
        "M3": [bot_msg(config.META_ERLEDIGT_FRAGE,
                       {"kw": 42, "jahr": 2026, "mitglied": "M3", "art": "ausgelost"},
                       reaktionen=["white_check_mark"])],
    }
    writes, dms, week_pages = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0", "M1", "M2", "M3"],
        reagierende={"UM0", "UM1"}, verlaeufe=gefragt)

    updates = [w for w in writes if w[0] == "update"]
    check("genau ein Notion-Update", len(updates), 1)
    check("M2 ist raus (keine Antwort)", "M2" in updates[0][2], False)
    check("M3 bleibt drin (per PM bestaetigt)", "M3" in updates[0][2], True)
    check("die Besen-Klicker bleiben drin",
          [mid for mid in ("M0", "M1") if mid in updates[0][2]], ["M0", "M1"])
    check("Status auf Erledigt",
          [w for w in writes if w[0] == "status"], [("status", "p42", config.WEEK_STATUS_DONE)])
    check("nur der Ausgetragene bekommt eine Abschluss-PM",
          [(d[0], d[1]) for d in dms], [("M2", config.META_ERLEDIGT_ABSCHLUSS)])
    check("Cache kennt die neue Belegung",
          week_pages["by_week"][(42, 2026)]["member_count"], 3)

    print("\n=== Erledigt: PM-Nein traegt aus, mit Begruendung ===")
    nein = {"M2": [bot_msg(config.META_ERLEDIGT_FRAGE,
                           {"kw": 42, "jahr": 2026, "mitglied": "M2", "art": "ausgelost"},
                           reaktionen=["x"])]}
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0", "M2"], reagierende={"UM0"}, verlaeufe=nein)
    updates = [w for w in writes if w[0] == "update"]
    check("M2 ist raus", updates[0][2], ["M0"])
    check("Begruendung nennt die Nachfrage", "❌" in dms[0][2], True)

    print("\n=== Erledigt: unerwarteter Helfer ===")
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=43, member_ids=["M0"], reagierende={"UM0", "UM5"}, verlaeufe={})
    check("Nachfrage an den Helfer", [(d[0], d[1]) for d in dms],
          [("M5", config.META_ERLEDIGT_FRAGE)])
    check("... und zwar die Zusatz-Variante", "mitgeputzt" in dms[0][2], True)

    ja = {"M5": [bot_msg(config.META_ERLEDIGT_FRAGE,
                         {"kw": 42, "jahr": 2026, "mitglied": "M5", "art": "zusatz"},
                         reaktionen=["white_check_mark"])]}
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0"], reagierende={"UM0", "UM5"}, verlaeufe=ja)
    updates = [w for w in writes if w[0] == "update"]
    check("Helfer wird eingetragen", updates[0][2], ["M0", "M5"])
    check("... und bekommt Bescheid",
          [(d[0], d[1]) for d in dms], [("M5", config.META_ERLEDIGT_ABSCHLUSS)])

    print("\n=== Erledigt: ohne Wochennachricht wird niemand ausgetragen ===")
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0", "M1"], reagierende=set(), verlaeufe={},
        nachricht=False)
    check("kein Mitglieder-Update", [w for w in writes if w[0] == "update"], [])
    check("keine PMs", dms, [])
    check("Woche wird trotzdem geschlossen",
          [w for w in writes if w[0] == "status"],
          [("status", "p42", config.WEEK_STATUS_DONE)])

    print("\n=== Erledigt: verpasster Lauf verkuerzt die Frist nicht ===")
    # KW 42 ist laengst faellig, es wurde aber nie gefragt (ausgefallener Montag).
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0", "M1"], reagierende={"UM0"}, verlaeufe={})
    check("erst mal nur fragen", [(d[0], d[1]) for d in dms],
          [("M1", config.META_ERLEDIGT_FRAGE)])
    check("kein Abschluss im selben Lauf", writes, [])

    print("\n=== Erledigt: alles bestaetigt ===")
    writes, dms, _ = _tracking_lauf(
        44, woche_kw=42, member_ids=["M0", "M1"], reagierende={"UM0", "UM1"}, verlaeufe={})
    check("kein Mitglieder-Update noetig", [w for w in writes if w[0] == "update"], [])
    check("keine PMs", dms, [])
    check("nur der Status wird gesetzt",
          [w for w in writes if w[0] == "status"],
          [("status", "p42", config.WEEK_STATUS_DONE)])


def test_nachrichtenfamilien():
    """Erledigt-PMs duerfen den Reschedule-Verlauf nicht abriegeln — und umgekehrt.

    Beide Auswertungen arbeiten nach "die neueste eigene Nachricht entscheidet".
    Ohne Trennung waere eine Nachfrage 'warst du putzen?' der neueste Anker und
    ein ❌ auf einer aelteren Auslos-DM fuer immer wirkungslos.
    """
    print("\n=== Nachrichtenfamilien trennen ===")

    auslosung = bot_msg(config.META_AUSLOSUNG,
                        {"kw": 45, "jahr": 2026, "mitglied": "M0"}, ["x"], ts="1")
    erledigt = bot_msg(config.META_ERLEDIGT_FRAGE,
                       {"kw": 43, "jahr": 2026, "mitglied": "M0"}, ts="5")
    verlauf = [erledigt, auslosung]  # neueste zuerst

    check("Reschedule sieht die Erledigt-PM nicht",
          reschedule.naechster_zustand(reschedule.verlauf_fuer(verlauf, "M0"),
                                       {(45, 2026)}),
          ("absage", (45, 2026)))

    m0 = member("M0")
    check("Erledigt sieht die Auslos-DM nicht",
          tracking.antwort_auf_nachfrage(verlauf, m0, 43, 2026), (None, True))
    check("... und findet die Antwort in seiner eigenen Familie",
          tracking.antwort_auf_nachfrage(
              [bot_msg(config.META_ERLEDIGT_FRAGE,
                       {"kw": 43, "jahr": 2026, "mitglied": "M0"},
                       reaktionen=["white_check_mark"])], m0, 43, 2026),
          ("ja", True))
    check("fremde Woche zaehlt nicht als gefragt",
          tracking.antwort_auf_nachfrage(verlauf, m0, 42, 2026), (None, False))


def test_wochennachricht_metadata():
    print("\n=== Wochennachricht traegt Metadata und Besen ===")

    posts = []
    original = (slack_utils.post_channel, slack_utils.get_slack_user_id)
    slack_utils.post_channel = lambda text, channel=None, metadata=None, reaktionen=(): (
        posts.append((text, metadata, tuple(reaktionen))) or "1.0"
    )
    slack_utils.get_slack_user_id = lambda email: "U1"
    try:
        crew = [member("M0")]
        woche = {"page_id": "p", "page_url": "u", "kw": 44, "year": 2026,
                 "member_ids": ["M0"], "member_count": 1, "status": "Crew voll",
                 "archiv": False}
        week_pages = {"by_week": {(44, 2026): woche}, "by_page_id": {"p": woche}}
        scheduler.remind_current_week(week_pages, {"M0": crew[0]}, 44, 2026)
    finally:
        slack_utils.post_channel, slack_utils.get_slack_user_id = original

    check("genau eine Nachricht", len(posts), 1)
    text, metadata, reaktionen = posts[0]
    check("Metadata nennt KW und Jahr", metadata["event_payload"], {"kw": 44, "jahr": 2026})
    check("Event-Typ ist die Wochennachricht", metadata["event_type"], config.META_WOCHE)
    check("Besen vorgesetzt", reaktionen, (config.PUTZ_REACTION,))
    check("Text erklaert den Besen", f":{config.PUTZ_REACTION}:" in text, True)
    check("... und nennt kein Daumen-Emoji", "thumbsup" in text, False)


def main():
    test_cycles()
    test_raffle()
    test_enrich()
    test_plan_flow()
    test_draw_flow()
    test_reschedule_logik()
    test_poll_flow()
    test_eigene_reaktionen_filtern()
    test_reaktionen_vorsetzen()
    test_post_channel_schalter()
    test_tracking_fenster()
    test_tracking_abgleich()
    test_nachrichtenfamilien()
    test_wochennachricht_metadata()
    test_filter_umfang()
    test_clean_string()
    test_tagcheck()

    print("\n" + "=" * 50)
    if FAILS:
        print(f"❌ {len(FAILS)} Test(s) fehlgeschlagen:")
        for failure in FAILS:
            print(failure)
        return 1
    print("✅ Alle Tests bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
