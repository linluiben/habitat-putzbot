"""Reschedule-Flow: ❌-Reaktionen einsammeln und Mitglieder umtragen.

Statt eines Webhook-Servers fragt der Bot mehrmals täglich bei Slack nach,
ob jemand auf seine Auslos-DM reagiert hat (`run_poll`). Die Zuordnung
"welche DM gehört zu welcher Woche" hängt als Slack-Message-Metadata an der
Nachricht selbst — deshalb braucht dieser Ablauf keinen eigenen Speicher.

Ablauf pro Mitglied, das in einer zukünftigen Woche eingetragen ist:

1. Neueste Bot-Nachricht im DM-Verlauf suchen, die von uns stammt.
2. Ist das eine Auslosung mit ❌  -> nach der Wunschwoche fragen.
3. Ist das eine Frage und darunter steht eine Antwort -> Woche prüfen und
   umtragen (bzw. bei Unsinn nochmal fragen).

Der Anker "neueste eigene Nachricht" sorgt nebenbei dafür, dass eine einmal
bearbeitete Reaktion nicht bei jedem Poll erneut greift: sobald wir geantwortet
haben, ist unsere Antwort die neueste Nachricht.

Ausgetragen wird bewusst **erst**, wenn eine gültige Zielwoche feststeht — sonst
stünde die alte Woche unbesetzt da, falls nie eine Antwort kommt.
"""

import re

import cycles
import notion
import scheduler
import slack_utils
from config import (
    CREW_SIZE,
    CYCLE_LENGTH_WEEKS,
    ERLEDIGT_EVENTS,
    META_AUSLOSUNG,
    META_BESTAETIGUNG,
    META_FRAGE,
    RESCHEDULE_MAX_CYCLES_AHEAD,
    WEEK_STATUS_BLOCKED,
    debug,
)

MAX_WOCHEN_VORAUS = RESCHEDULE_MAX_CYCLES_AHEAD * CYCLE_LENGTH_WEEKS


# --------------------------------------------------------------- reine Logik

def parse_wochennummer(text):
    """Erste plausible Kalenderwoche aus einer Freitext-Antwort ziehen.

    Akzeptiert "22", "KW 22", "ich würde gerne in 22" — alles, wo eine Zahl
    zwischen 1 und 53 auftaucht.
    """
    for treffer in re.findall(r"\d{1,2}", text or ""):
        zahl = int(treffer)
        if 1 <= zahl <= 53:
            return zahl
    return None


def kurzfassung(text, laenge=40):
    """Antwort des Mitglieds für die Rückmeldung aufbereiten.

    Wird zitiert, um zu zeigen, worauf sich die Absage bezieht — deshalb:
    auf eine Zeile zusammenziehen, an der Grenze mit Auslassung statt hartem
    Schnitt mitten im Wort, und Backticks entfernen, weil der Text in Slack in
    einem Code-Span steht und ein Backtick die Formatierung zerlegen würde.
    """
    zusammen = " ".join((text or "").split()).replace("`", "'")
    if len(zusammen) <= laenge:
        return zusammen
    return zusammen[:laenge].rstrip() + "…"


def zielwoche_bestimmen(ziel_kw, heute_kw, heute_jahr):
    """Bloße KW-Zahl auf (kw, jahr) abbilden: das nächste Vorkommen in der Zukunft.

    Gibt (kw, jahr) zurück oder (None, Begründung).
    """
    if ziel_kw is None:
        return None, "das sieht nicht nach einer Kalenderwoche aus"

    jahr = heute_jahr if ziel_kw > heute_kw else heute_jahr + 1
    if ziel_kw > cycles.iso_weeks_in_year(jahr):
        return None, f"das Jahr {jahr} hat gar keine KW {ziel_kw}"

    abstand = cycles.weeks_between(heute_kw, heute_jahr, ziel_kw, jahr)
    if abstand <= 0:
        return None, "die Woche liegt schon in der Vergangenheit"
    if abstand > MAX_WOCHEN_VORAUS:
        return None, (
            f"das ist mehr als {RESCHEDULE_MAX_CYCLES_AHEAD} Zyklen im Voraus — "
            f"such dir bitte etwas Näheres aus"
        )
    return (ziel_kw, jahr), None


def ist_platz_frei(belegung):
    """Passt noch jemand in eine Woche, die schon `belegung` Leute hat?"""
    return belegung < CREW_SIZE


def verlauf_fuer(verlauf, member_id):
    """Bot-Nachrichten aussortieren, die einem anderen Mitglied gelten.

    Produktiv hat jedes Mitglied seinen eigenen DM-Kanal, hier fällt also nichts
    weg. Beim Testen mit `SLACK_TEST_USER_ID` gehen dagegen ALLE DMs an dieselbe
    Person: ohne diesen Filter wäre die neueste Bot-Nachricht im Kanal
    womöglich die eines anderen Mitglieds, und ein einziges ❌ würde den Tausch
    für die ganze Crew der Woche auslösen.

    Nachrichten des Mitglieds selbst (die Antworten) tragen keine Metadata und
    bleiben deshalb immer drin — genauso wie Bot-Nachrichten ohne Zuordnung,
    damit der Anker "neueste eigene Nachricht gewinnt" nicht aufgeweicht wird.

    Aus demselben Grund fliegen Nachrichten der Erledigt-Familie raus: eine
    Nachfrage "warst du in KW 36 putzen?" wäre sonst die neueste Bot-Nachricht
    und würde diesen Anker besetzen — ein ❌ auf einer älteren Auslos-DM bliebe
    dann für immer wirkungslos. Jede Auswertung sieht nur ihre eigene Familie.
    """
    return [
        eintrag
        for eintrag in verlauf
        if not eintrag["ist_vom_bot"]
        or (eintrag["payload"].get("mitglied") in (None, member_id)
            and eintrag["event_type"] not in ERLEDIGT_EVENTS)
    ]


def naechster_zustand(verlauf, aktuelle_wochen):
    """Aus dem DM-Verlauf ableiten, was jetzt zu tun ist.

    `aktuelle_wochen` ist die Menge der (kw, jahr), in denen das Mitglied
    gerade eingetragen ist. Rückgabe:
      ("absage", (kw, jahr))   -> hat mit ❌ reagiert, Wunschwoche erfragen
      ("antwort", (text, (kw, jahr))) -> hat auf unsere Frage geantwortet
      (None, None)             -> nichts zu tun
    """
    for index, eintrag in enumerate(verlauf):  # neueste zuerst
        if not eintrag["ist_vom_bot"]:
            continue

        # Die neueste eigene Nachricht entscheidet, und zwar auch dann, wenn sie
        # keine Metadata trägt: eine Bestätigung ("Erledigt, du bist jetzt in
        # KW 22") heißt, dass der Vorgang abgeschlossen ist. Ohne dieses
        # Abbrechen fände die Schleife darunter erneut die alte Frage samt
        # Antwort und würde denselben Tausch ein zweites Mal ausführen.
        if eintrag["event_type"] == META_FRAGE:
            # Antworten sind neuer als die Frage, stehen also VOR ihr in der Liste
            antworten = [e for e in verlauf[:index] if not e["ist_vom_bot"]]
            if not antworten:
                return None, None
            payload = eintrag["payload"]
            woche = (payload.get("kw"), payload.get("jahr"))
            return "antwort", (antworten[0]["text"], woche)

        if eintrag["event_type"] == META_AUSLOSUNG:
            if slack_utils.reaktion_auf(eintrag) != "nein":
                return None, None
            payload = eintrag["payload"]
            woche = (payload.get("kw"), payload.get("jahr"))
            # Nur reagieren, wenn die Woche noch aktuell ist — eine alte Absage
            # zu einer längst getauschten Woche ist erledigt.
            if woche not in aktuelle_wochen:
                debug(f"❌ auf KW {woche[0]}, aber dort nicht mehr eingetragen — ignoriert.")
                return None, None
            return "absage", woche

        # Irgendeine andere eigene Nachricht (z.B. die Tausch-Bestätigung).
        # Falls hier eine Auslos-DM ohne Metadata landet, käme die Zuordnung
        # nicht zustande und der Poll täte stillschweigend nichts — deshalb
        # sichtbar machen statt kommentarlos aufhören.
        if not eintrag["event_type"] and "ausgelost" in eintrag["text"]:
            debug(
                "⚠️ Auslos-DM ohne Metadata gefunden — Zuordnung nicht möglich. "
                "Wurde sie von einer älteren Bot-Version verschickt?"
            )
        return None, None

    return None, None


# ------------------------------------------------------------------- Aktionen

def _beispiel_kw(heute_kw, heute_jahr):
    """Plausible Beispielwoche für die Nachfrage-Texte."""
    kw = heute_kw + 6
    jahr_laenge = cycles.iso_weeks_in_year(heute_jahr)
    return kw - jahr_laenge if kw > jahr_laenge else kw


def _frage_stellen(member, kw, jahr, heute_kw, heute_jahr, text=None):
    """Nachfrage schicken und dabei den Anker für den nächsten Poll setzen."""
    beispiel = _beispiel_kw(heute_kw, heute_jahr)
    slack_utils.send_dm(
        member,
        text or slack_utils.build_reschedule_frage(member, kw, beispiel),
        metadata={
            "event_type": META_FRAGE,
            "event_payload": {"kw": kw, "jahr": jahr, "mitglied": member["id"]},
        },
    )


def _umtragen(member, alte_woche, ziel_kw, ziel_jahr, week_pages, members, lookup,
              heute_kw, heute_jahr):
    """Mitglied aus der alten in die neue Woche verschieben."""
    ziel = notion.notion_lookup(ziel_kw, ziel_jahr, week_pages)
    beispiel = _beispiel_kw(heute_kw, heute_jahr)

    if ziel["archiv"] or ziel["status"] == WEEK_STATUS_BLOCKED:
        _frage_stellen(
            member, alte_woche["kw"], alte_woche["year"], heute_kw, heute_jahr,
            text=slack_utils.build_reschedule_fehler(
                member, f"KW {ziel_kw}", "diese Woche ist gesperrt", beispiel
            ),
        )
        return False

    if not ist_platz_frei(ziel["member_count"]):
        _frage_stellen(
            member, alte_woche["kw"], alte_woche["year"], heute_kw, heute_jahr,
            text=slack_utils.build_reschedule_fehler(
                member,
                f"KW {ziel_kw}",
                f"da stehen schon {ziel['member_count']} Leute drin, die Woche ist voll",
                beispiel,
                link=ziel["page_url"],
            ),
        )
        return False

    print(f"   ↔️ {member['name']}: KW {alte_woche['kw']} → KW {ziel_kw}/{ziel_jahr}")

    # 1. aus der alten Woche austragen
    rest_alt = [mid for mid in alte_woche["member_ids"] if mid != member["id"]]
    notion.update_page_members(alte_woche["page_id"], rest_alt)
    scheduler.cache_week(week_pages, dict(alte_woche, member_ids=rest_alt,
                                          member_count=len(rest_alt)))

    # 2. in die neue Woche eintragen (Seite ggf. anlegen)
    neue_ids = ziel["member_ids"] + [member["id"]]
    ziel_url = ziel["page_url"]
    if ziel["page_status"] == "exists":
        notion.update_page_members(ziel["page_id"], neue_ids)
        scheduler.setze_status(ziel, len(neue_ids))
        scheduler.cache_week(week_pages, dict(ziel, member_ids=neue_ids,
                                              member_count=len(neue_ids)))
    else:
        neue_id, neue_url = notion.create_week_page(
            ziel_kw, ziel_jahr, neue_ids, status=scheduler.status_fuer(len(neue_ids))
        )
        ziel_url = neue_url
        if neue_id:
            scheduler.cache_week(week_pages, {
                "page_id": neue_id, "page_url": neue_url, "kw": ziel_kw,
                "year": ziel_jahr, "member_ids": neue_ids,
                "member_count": len(neue_ids),
                "status": scheduler.status_fuer(len(neue_ids)), "archiv": False,
            })

    # 3. Mitglied informieren
    slack_utils.send_dm(
        member,
        slack_utils.build_reschedule_ok(member, alte_woche["kw"], ziel_kw, ziel_url),
        metadata={
            "event_type": META_BESTAETIGUNG,
            "event_payload": {"kw": ziel_kw, "jahr": ziel_jahr, "mitglied": member["id"]},
        },
    )

    # 4. Alte Woche ggf. wieder auffüllen
    aktuelle_alte = week_pages["by_week"].get((alte_woche["kw"], alte_woche["year"]))
    if aktuelle_alte and aktuelle_alte["member_count"] < CREW_SIZE:
        print(f"   🎲 KW {alte_woche['kw']} ist unterbesetzt — lose nach.")
        scheduler.fill_week(
            dict(aktuelle_alte, page_status="exists"),
            members, week_pages, lookup,
            exclude_ids={member["id"]},
        )
    return True


# ------------------------------------------------------------------ Poll-Lauf

def run_poll(week_pages, members, lookup, heute_kw, heute_jahr):
    """Alle Mitglieder mit anstehenden Einsätzen auf Reaktionen prüfen."""
    print(f"\n📮 Poll — prüfe Reaktionen (Stand KW {heute_kw}/{heute_jahr})")

    zukunft = [
        woche
        for woche in week_pages["by_week"].values()
        if not woche["archiv"]
        and cycles.weeks_between(heute_kw, heute_jahr, woche["kw"], woche["year"]) > 0
    ]
    if not zukunft:
        print("   Keine zukünftigen Wochen — nichts zu prüfen.")
        return 0

    wochen_pro_mitglied = {}
    for woche in zukunft:
        for mid in woche["member_ids"]:
            wochen_pro_mitglied.setdefault(mid, []).append(woche)

    print(f"   {len(wochen_pro_mitglied)} Mitglieder in {len(zukunft)} anstehenden Wochen.")

    aktionen = 0
    for mid, wochen in wochen_pro_mitglied.items():
        member = lookup.get(mid)
        if not member:
            debug(f"Mitglied {mid} nicht in der Mitgliederliste — übersprungen.")
            continue

        verlauf = slack_utils.read_dm_history(member)
        if not verlauf:
            continue

        aktuelle = {(w["kw"], w["year"]) for w in wochen}
        was, daten = naechster_zustand(verlauf_fuer(verlauf, mid), aktuelle)

        if was == "absage":
            kw, jahr = daten
            print(f"   ❌ {member['name']} möchte aus KW {kw} raus — frage nach.")
            _frage_stellen(member, kw, jahr, heute_kw, heute_jahr)
            aktionen += 1

        elif was == "antwort":
            text, (kw, jahr) = daten
            alte_woche = next(
                (w for w in wochen if (w["kw"], w["year"]) == (kw, jahr)), None
            )
            if not alte_woche:
                debug(f"{member['name']} hat geantwortet, ist aber nicht mehr in KW {kw}.")
                continue

            ziel, grund = zielwoche_bestimmen(
                parse_wochennummer(text), heute_kw, heute_jahr
            )
            if not ziel:
                zitat = kurzfassung(text)
                print(f"   ↩️ {member['name']} antwortete '{zitat}' — {grund}")
                _frage_stellen(
                    member, kw, jahr, heute_kw, heute_jahr,
                    text=slack_utils.build_reschedule_fehler(
                        member, zitat, grund, _beispiel_kw(heute_kw, heute_jahr),
                    ),
                )
                aktionen += 1
                continue

            # Zählt in beiden Fällen: entweder wurde umgetragen oder das
            # Mitglied hat eine Absage mit erneuter Nachfrage bekommen.
            _umtragen(member, alte_woche, ziel[0], ziel[1], week_pages,
                      members, lookup, heute_kw, heute_jahr)
            aktionen += 1

    if aktionen == 0:
        print("   Nichts Neues.")
    else:
        print(f"\n   {aktionen} Vorgang/Vorgänge bearbeitet.")
    return aktionen
