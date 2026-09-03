"""Putzbot — Einstiegspunkt.

Betriebsarten:

    python main.py          wöchentlich (montags): Erinnerung, am Zyklusende
                            zusätzlich Planung des Folgezyklus
    python main.py poll     mehrmals täglich: schaut nach ✅/❌-Reaktionen auf
                            die Auslos-DMs und wickelt Tauschwünsche ab
    python main.py erledigt montags: gleicht ab, wer letzte Woche wirklich
                            geputzt hat, und schließt Wochen ab, deren
                            Nachfrage-Frist abgelaufen ist

Dazu zwei Modi für den Umstieg von V2 auf V3, gedacht für den manuellen Aufruf:

    python main.py draw     nur die laufende Woche auslosen, eine Kanal-
                            nachricht, KEINE DMs und keine Zyklusplanung —
                            das alte Verfahren mit der neuen Auslosungslogik
    python main.py plan     nur den Folgezyklus planen (mit DMs und
                            Reschedule), ohne die Wochenerinnerung
    python main.py tags     Diagnose vor dem Cutover: für jedes losbare
                            Mitglied prüfen, ob sich die Slack-ID über die
                            E-Mail finden lässt. Ergebnis kommt als DM,
                            schreibt nichts.

`weekly` ist die Summe aus `erledigt`, Erinnerung und — am Zyklusende — `plan`.
Die Einzelmodi gibt es, damit sich das an verschiedenen Tagen auslösen lässt.

Die eigentliche Logik liegt in den Modulen; hier wird nur orchestriert.
"""

import sys

import cycles
import notion
import reschedule
import scheduler
import tagcheck
import tracking
from config import (
    DATENQUELLE,
    DEBUG,
    DRY_RUN,
    FORCE_PLAN,
    HEUTE_JAHR,
    HEUTE_KW,
    SLACK_TEST_USER_ID,
    SLACK_ZIEL,
    check_env,
)

MODI = ("weekly", "poll", "draw", "plan", "tags", "erledigt")


def _lade_daten(mit_kandidaten):
    """Wochen- und Mitgliederdaten laden. Gibt (week_pages, lookup, members) zurück."""
    week_pages = notion.get_week_pages()
    if week_pages is None:
        return None, None, None

    alle = notion.get_all_members()
    if alle is None:
        return None, None, None
    lookup = {member["id"]: member for member in alle}

    members = None
    if mit_kandidaten:
        members = notion.get_eligible_members()
        if members is None:
            return None, None, None

    return week_pages, lookup, members


def main(argv):
    modus = argv[1].lower() if len(argv) > 1 else "weekly"
    if modus not in MODI:
        print(f"❌ Unbekannter Modus '{modus}'. Erlaubt: {', '.join(MODI)}")
        return 2

    check_env()

    kw, year = cycles.current_week()
    cycle = cycles.cycle_of_week(kw)

    print(f"🤖 Putzbot [{modus}] — KW {kw}/{year} (Zyklus {cycle})")
    print(f"   Notion: {DATENQUELLE}   Slack: {SLACK_ZIEL}")
    if DRY_RUN:
        print("🧪 DRY RUN — keine Schreibzugriffe auf Notion oder Slack.")
    if DEBUG:
        print("🐛 DEBUG aktiv.")
    if SLACK_TEST_USER_ID:
        print(f"📮 Alle DMs gehen umgeleitet an {SLACK_TEST_USER_ID}.")
    if HEUTE_KW or HEUTE_JAHR:
        print(f"⏱️ ACHTUNG: Der Bot rechnet mit KW {kw}/{year} statt mit dem "
              f"echten Datum (HEUTE_KW/HEUTE_JAHR gesetzt).")

    # Auswahlwerte in Notion können umbenannt werden; die Filter hängen an den
    # Namen. Lieber hier abbrechen als mit halbem Kandidatenpool auslosen.
    if not notion.pruefe_filter_optionen():
        return 1

    if modus == "tags":
        # Reine Diagnose: kein Wochen-Lookup nötig, kein Schreibzugriff.
        # Bewusst der weitere Filter — geprüft wird, wer prinzipiell losbar ist,
        # nicht nur der heutige Topf.
        members = notion.get_taggable_members()
        if members is None:
            return 1
        code = tagcheck.run(members)
        print("\n✅ Fertig." if code == 0 else "")
        return code

    if modus == "poll":
        # Kandidatenliste wird nur gebraucht, falls durch einen Tausch
        # nachgelost werden muss — der Fall ist häufig genug, um sie zu laden.
        week_pages, lookup, members = _lade_daten(mit_kandidaten=True)
        if week_pages is None:
            return 1
        reschedule.run_poll(week_pages, members, lookup, kw, year)
        print("\n✅ Fertig.")
        return 0

    if modus == "erledigt":
        # Braucht nur die Zuordnung ID -> Mitglied, es wird nichts ausgelost.
        week_pages, lookup, _ = _lade_daten(mit_kandidaten=False)
        if week_pages is None:
            return 1
        tracking.run_abgleich(week_pages, lookup, kw, year)
        print("\n✅ Fertig.")
        return 0

    if modus in ("draw", "plan"):
        week_pages, lookup, members = _lade_daten(mit_kandidaten=True)
        if week_pages is None:
            return 1
        if modus == "draw":
            scheduler.draw_current_week(week_pages, members, lookup, kw, year)
        else:
            scheduler.plan_next_cycle(week_pages, members, lookup, kw, year)
        print("\n✅ Fertig.")
        return 0

    plan_faellig = scheduler.should_plan(kw, year, force=FORCE_PLAN)
    if plan_faellig:
        print("⏩ FORCE_PLAN aktiv." if FORCE_PLAN else "")

    week_pages, lookup, members = _lade_daten(mit_kandidaten=plan_faellig)
    if week_pages is None:
        return 1

    # Erst abgleichen, dann erinnern: der Abgleich wertet die Nachricht der
    # VORwoche aus, die Erinnerung postet die der laufenden. Andersherum stünde
    # die neue Nachricht schon im Verlauf, wenn die alte gesucht wird — das geht
    # zwar über die Metadata gut, ist aber unnötig verwirrend zu lesen.
    tracking.run_abgleich(week_pages, lookup, kw, year)

    scheduler.remind_current_week(week_pages, lookup, kw, year)

    if plan_faellig:
        scheduler.plan_next_cycle(week_pages, members, lookup, kw, year)
    else:
        last_kw = cycles.weeks_in_cycle(cycle, year)[-1]
        print(f"\n💤 Kein Plan-Lauf — der läuft erst in KW {last_kw} (Ende Zyklus {cycle}).")

    print("\n✅ Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
