"""Erledigt-Tracking: wer hat tatsächlich geputzt?

Bis hierher zählt der Bot nur *Zuteilungen*. Wer eingetragen war und nicht kam,
ist im System nicht von jemandem zu unterscheiden, der da war — und wird für die
Fairness-Rechnung sogar genauso behandelt ("hat ja letztens erst"). Dieses Modul
gleicht die Zuteilung mit der Realität ab und schreibt das Ergebnis in dieselbe
`Mitglieder`-Relation zurück.

**Eine** Relation, bewusst: wer nicht geputzt hat, wird aus der Wochenseite
ausgetragen, wer zusätzlich geputzt hat, kommt dazu. Dadurch korrigieren sich
`putz_count` und Schonfrist von selbst. Preis: "war ausgelost, hat nicht
geputzt" steht danach nur noch in Slack und in der Notion-Seitenhistorie.

Ablauf über drei Montage, am Beispiel der Putzwoche KW N:

    Mo KW N      Wochennachricht in den Kanal, mit Metadata {kw, jahr} und
                 vorgesetztem :broom:. Wer putzt, klickt drauf.
    Mo KW N+1    ABGLEICH: Reaktionen lesen. Ausgeloste ohne Reaktion und
                 Reagierende ohne Zuteilung bekommen eine Nachfrage-PM mit
                 Frist. Notion wird noch nicht angefasst.
    Mo KW N+2    ABSCHLUSS: Frist ist um. Notion wird EINMAL geschrieben,
                 Status auf 'Erledigt'. Default für alle, die nie geantwortet
                 haben: Ausgeloste fliegen raus, Zusätzliche kommen nicht rein.

Der Zustand steckt komplett in Slack und Notion — kein eigener Speicher:
Fälligkeit ergibt sich aus der Wochendistanz, "schon gefragt?" aus dem
Vorhandensein der Nachfrage-PM, "schon abgeschlossen?" aus dem Notion-Status.
"""

from datetime import date, datetime, timedelta, timezone

import cycles
import notion
import scheduler
import slack_utils
from config import (
    META_ERLEDIGT_ABSCHLUSS,
    META_ERLEDIGT_FRAGE,
    PREFILL_REACTIONS,
    PUTZ_REACTION,
    TRACKING_DEADLINE_WEEKS,
    TRACKING_ENABLED,
    TRACKING_MAX_LOOKBACK_WEEKS,
    TRACKING_START_KW,
    TRACKING_START_YEAR,
    WEEK_STATUS_BLOCKED,
    WEEK_STATUS_DONE,
    debug,
)


# ------------------------------------------------------------------ Hilfsmittel

def montag(kw, year):
    """Datum des Montags einer Kalenderwoche (KW 53 in einem 52-Wochen-Jahr geklemmt)."""
    try:
        return date.fromisocalendar(year, kw, 1)
    except ValueError:
        return date.fromisocalendar(year, cycles.iso_weeks_in_year(year), 1)


def frist_text(heute_kw, heute_jahr):
    """Wann läuft die Nachfrage-Frist ab? Als Datum, nicht als KW.

    In einer PM ist "bis zum 14.09." unmissverständlich; "bis KW 38" muss man
    erst nachschlagen.
    """
    ende = montag(heute_kw, heute_jahr) + timedelta(weeks=TRACKING_DEADLINE_WEEKS)
    return f"Montag, {ende.strftime('%d.%m.')}"


def _oldest_ts(kw, year):
    """Slack-Timestamp, ab dem die Kanal-Historie gelesen wird.

    Ein Tag Puffer vor dem Montag der ältesten fraglichen Woche: die
    Wochennachricht wird montags früh gepostet, und ein knapper Schnitt genau
    auf Mitternacht würde sie bei Zeitzonen-Rundungen verlieren.
    """
    start = datetime.combine(montag(kw, year) - timedelta(days=1),
                             datetime.min.time(), tzinfo=timezone.utc)
    return f"{start.timestamp():.6f}"


def offene_wochen(week_pages, heute_kw, heute_jahr):
    """Vergangene Wochen, die noch auf ihren Erledigt-Abgleich warten.

    Absichtlich eng begrenzt. Ohne `TRACKING_START_*` würde der erste Lauf die
    komplette Historie einsammeln und aus jeder alten Woche alle austragen —
    deren Kanalnachrichten tragen ja gar keine Metadata, es sähe also aus, als
    hätte nie jemand geputzt. `TRACKING_MAX_LOOKBACK_WEEKS` deckelt dasselbe
    Risiko nach einem längeren Ausfall.
    """
    treffer = []
    for woche in week_pages["by_week"].values():
        abstand = cycles.weeks_between(heute_kw, heute_jahr, woche["kw"], woche["year"])
        if abstand >= 0:
            continue  # laufende oder künftige Woche
        if abstand < -TRACKING_MAX_LOOKBACK_WEEKS:
            continue
        if cycles.weeks_between(TRACKING_START_KW, TRACKING_START_YEAR,
                                woche["kw"], woche["year"]) < 0:
            continue
        if woche["archiv"] or woche["status"] in (WEEK_STATUS_BLOCKED, WEEK_STATUS_DONE):
            continue
        treffer.append((woche, abstand))

    treffer.sort(key=lambda paar: paar[1])  # älteste zuerst
    return treffer


def _mitglied_zu_slack_id(user_id, lookup):
    """Unerwartete Reagierende über ihre Slack-E-Mail einem Mitglied zuordnen."""
    email = slack_utils.email_fuer_user(user_id)
    if not email:
        return None
    email = email.lower()
    for member in lookup.values():
        if (member.get("email") or "").lower() == email:
            return member
    return None


def antwort_auf_nachfrage(verlauf, member, kw, jahr):
    """('ja'|'nein'|None, schon_gefragt) aus dem DM-Verlauf ziehen.

    Filtert zuerst auf die eigene Nachrichtenfamilie: der Reschedule-Flow legt
    seine Nachrichten im selben DM-Kanal ab, und dessen Bestätigungen wären hier
    nur Rauschen. Umgekehrt gilt dasselbe — siehe `reschedule.verlauf_fuer`.
    """
    for eintrag in verlauf:  # neueste zuerst
        if not eintrag["ist_vom_bot"]:
            continue
        if eintrag["event_type"] != META_ERLEDIGT_FRAGE:
            continue
        payload = eintrag["payload"]
        if (payload.get("kw"), payload.get("jahr")) != (kw, jahr):
            continue
        if payload.get("mitglied") not in (None, member["id"]):
            continue
        return slack_utils.reaktion_auf(eintrag), True
    return None, False


# --------------------------------------------------------------------- Abgleich

def _nachfrage(member, kw, jahr, art, heute_kw, heute_jahr):
    slack_utils.send_dm(
        member,
        slack_utils.build_erledigt_frage(member, kw, art, frist_text(heute_kw, heute_jahr)),
        metadata=slack_utils.erledigt_metadata(META_ERLEDIGT_FRAGE, member, kw, jahr, art),
        reaktionen=PREFILL_REACTIONS,
    )


def _abschliessen(woche, behalten, dazu, week_pages, lookup, gruende):
    """Ergebnis nach Notion schreiben und die Betroffenen informieren."""
    alt = woche["member_ids"]
    neu = [mid for mid in alt if mid in behalten] + [
        mid for mid in dazu if mid not in alt
    ]

    raus = [mid for mid in alt if mid not in neu]
    rein = [mid for mid in neu if mid not in alt]

    if neu != alt:
        notion.update_page_members(woche["page_id"], neu)
    else:
        print("   ✅ Alles bestätigt — nichts zu ändern.")

    notion.set_week_status(woche["page_id"], WEEK_STATUS_DONE)
    scheduler.cache_week(week_pages, dict(woche, member_ids=neu, member_count=len(neu),
                                          status=WEEK_STATUS_DONE))

    for mid in raus:
        member = lookup.get(mid)
        if not member:
            continue
        slack_utils.send_dm(
            member,
            slack_utils.build_erledigt_ausgetragen(
                member, woche["kw"], gruende.get(mid, "es kam keine Rückmeldung")
            ),
            metadata=slack_utils.erledigt_metadata(
                META_ERLEDIGT_ABSCHLUSS, member, woche["kw"], woche["year"]
            ),
        )
    for mid in rein:
        member = lookup.get(mid)
        if not member:
            continue
        slack_utils.send_dm(
            member,
            slack_utils.build_erledigt_eingetragen(member, woche["kw"], woche["page_url"]),
            metadata=slack_utils.erledigt_metadata(
                META_ERLEDIGT_ABSCHLUSS, member, woche["kw"], woche["year"]
            ),
        )

    wort = "Putzeinsatz" if len(neu) == 1 else "Putzeinsätze"
    print(f"   🧹 KW {woche['kw']} abgeschlossen: {len(neu)} {wort} "
          f"(+{len(rein)} / -{len(raus)}).")


def verarbeite_woche(woche, ts, abstand, week_pages, lookup, heute_kw, heute_jahr):
    """Eine vergangene Woche abgleichen und, wenn die Frist um ist, abschließen."""
    kw, jahr = woche["kw"], woche["year"]
    faellig = -abstand > TRACKING_DEADLINE_WEEKS

    print(f"\n   ── KW {kw}/{jahr} ──")

    if ts is None:
        # Ohne Nachricht gibt es keine Reaktionen — und "keine Reaktionen" darf
        # NICHT als "niemand hat geputzt" durchgehen. Also: nichts austragen,
        # nach Fristablauf nur den Status setzen, damit es nicht ewig meckert.
        print("   ⚠️ Keine Wochennachricht mit Metadata gefunden — "
              "es wird niemand ausgetragen.")
        if faellig:
            notion.set_week_status(woche["page_id"], WEEK_STATUS_DONE)
            scheduler.cache_week(week_pages, dict(woche, status=WEEK_STATUS_DONE))
        return 0

    reagierende = slack_utils.reagierende_user(ts, PUTZ_REACTION)
    if reagierende is None:
        print("   ⏭️ Reaktionen nicht lesbar — nächster Lauf versucht es erneut.")
        return 0

    crew = [lookup[mid] for mid in woche["member_ids"] if mid in lookup]
    unbekannt = [mid for mid in woche["member_ids"] if mid not in lookup]
    if unbekannt:
        print(f"   ⚠️ {len(unbekannt)} eingetragene ID(s) ohne Mitglied — bleiben unangetastet.")

    bestaetigt, offen, crew_ids = [], [], set()
    for member in crew:
        slack_id = slack_utils.slack_id_fuer(member)
        if slack_id:
            crew_ids.add(slack_id)
        (bestaetigt if slack_id and slack_id in reagierende else offen).append(member)

    zusatz = []
    for user_id in sorted(reagierende - crew_ids):
        member = _mitglied_zu_slack_id(user_id, lookup)
        if member:
            zusatz.append(member)
        else:
            print(f"   ⚠️ Reaktion von {user_id} — kein Mitglied dazu gefunden.")

    print(f"   :{PUTZ_REACTION}: bestätigt: {len(bestaetigt)} von {len(crew)}"
          + (f", zusätzlich: {len(zusatz)}" if zusatz else ""))

    behalten = {member["id"] for member in bestaetigt} | set(unbekannt)
    dazu, gruende = [], {}
    neu_gefragt = 0

    for member, art in [(m, "ausgelost") for m in offen] + [(m, "zusatz") for m in zusatz]:
        antwort, gefragt = antwort_auf_nachfrage(
            slack_utils.read_dm_history(member), member, kw, jahr
        )
        if antwort == "ja":
            if art == "ausgelost":
                behalten.add(member["id"])
            else:
                dazu.append(member["id"])
            debug(f"{member['name']} hat KW {kw} per PM bestätigt ({art}).")
        elif antwort == "nein":
            if art == "ausgelost":
                gruende[member["id"]] = "du hast auf meine Nachfrage mit ❌ geantwortet"
            debug(f"{member['name']} hat KW {kw} per PM verneint ({art}).")
        elif not gefragt:
            print(f"   ❓ Nachfrage an {member['name']} ({art}).")
            _nachfrage(member, kw, jahr, art, heute_kw, heute_jahr)
            neu_gefragt += 1
        else:
            debug(f"{member['name']} hat auf die Nachfrage zu KW {kw} noch nicht geantwortet.")

    if not faellig:
        print("   ⏳ Frist läuft noch — Abschluss beim nächsten Lauf.")
        return neu_gefragt

    if neu_gefragt:
        # Kann nur passieren, wenn ein Montagslauf ausgefallen ist. Dann wurde
        # gerade eben zum ersten Mal gefragt — im selben Lauf abzuschließen
        # hieße, die Frist auf null zu setzen.
        print("   ⏳ Nachfrage ging gerade erst raus — Abschluss beim nächsten Lauf.")
        return neu_gefragt

    _abschliessen(woche, behalten, dazu, week_pages, lookup, gruende)
    return 1


def run_abgleich(week_pages, lookup, heute_kw, heute_jahr):
    """Erledigt-Abgleich für alle vergangenen Wochen im Auswertungsfenster."""
    if not TRACKING_ENABLED:
        debug("TRACKING_ENABLED=False — kein Erledigt-Abgleich.")
        return 0

    print(f"\n🧾 Erledigt-Abgleich (Stand KW {heute_kw}/{heute_jahr})")

    wochen = offene_wochen(week_pages, heute_kw, heute_jahr)
    if not wochen:
        print("   Keine offenen Wochen im Auswertungsfenster.")
        return 0

    aelteste = wochen[0][0]
    nachrichten = slack_utils.finde_wochennachrichten(
        oldest=_oldest_ts(aelteste["kw"], aelteste["year"])
    )
    if nachrichten is None:
        # Ohne Kanal-Historie lässt sich nichts entscheiden. Abbrechen statt
        # "keine Reaktion gefunden" anzunehmen — das würde reihenweise Leute
        # austragen, die längst geklickt haben.
        print("   ⏭️ Ohne Kanal-Historie kein Abgleich.")
        return 0

    aktionen = 0
    for woche, abstand in wochen:
        aktionen += verarbeite_woche(
            woche, nachrichten.get((woche["kw"], woche["year"])), abstand,
            week_pages, lookup, heute_kw, heute_jahr,
        )

    if aktionen == 0:
        print("\n   Nichts zu tun.")
    return aktionen
