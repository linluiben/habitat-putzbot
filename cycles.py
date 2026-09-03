"""Zyklus- und Kalenderwochen-Mathematik.

Das Jahr ist in 13 Zyklen à 4 Wochen unterteilt: Zyklus 1 = KW 1-4, ...,
Zyklus 13 = KW 49-52. ISO-Jahre mit 53 Wochen hängen die KW 53 an Zyklus 13
an, statt einen 14. Zyklus zu eröffnen.

Reine Rechenlogik ohne Notion/Slack — deshalb ein eigenes Modul, damit sowohl
`raffle.py` (Aktualität) als auch `scheduler.py` (Planung) es importieren
können, ohne sich gegenseitig zu importieren.
"""

from datetime import date

from config import CYCLE_LENGTH_WEEKS, CYCLES_PER_YEAR, HEUTE_JAHR, HEUTE_KW


def iso_weeks_in_year(year):
    """52 oder 53. Der 28.12. liegt per ISO-Definition immer in der letzten Woche."""
    return date(year, 12, 28).isocalendar()[1]


def current_week():
    """(kw, jahr) für heute — als ISO-Jahr, das um den Jahreswechsel vom Kalenderjahr abweicht.

    `HEUTE_KW`/`HEUTE_JAHR` überschreiben das für Testläufe: das Erledigt-Tracking
    wertet vergangene Wochen aus und wäre sonst nur über mehrere echte Montage
    hinweg zu testen.
    """
    iso = date.today().isocalendar()
    return HEUTE_KW or iso[1], HEUTE_JAHR or iso[0]


def cycle_of_week(kw):
    """Zyklusnummer (1-13) einer Kalenderwoche. KW 53 landet in Zyklus 13."""
    return min(CYCLES_PER_YEAR, (kw + CYCLE_LENGTH_WEEKS - 1) // CYCLE_LENGTH_WEEKS)


def weeks_in_cycle(cycle, year):
    """Alle Kalenderwochen eines Zyklus. Der letzte Zyklus schluckt ggf. die KW 53."""
    start = (cycle - 1) * CYCLE_LENGTH_WEEKS + 1
    end = cycle * CYCLE_LENGTH_WEEKS
    if cycle == CYCLES_PER_YEAR:
        end = iso_weeks_in_year(year)
    return list(range(start, end + 1))


def is_last_week_of_cycle(kw, year):
    """Trigger-Bedingung für den Plan-Prozess."""
    return kw == weeks_in_cycle(cycle_of_week(kw), year)[-1]


def next_cycle(kw, year):
    """(zyklus, jahr) des Folgezyklus — nach Zyklus 13 kommt Zyklus 1 des Folgejahres."""
    cycle = cycle_of_week(kw)
    if cycle == CYCLES_PER_YEAR:
        return 1, year + 1
    return cycle + 1, year


def next_cycle_weeks(kw, year):
    """Liste von (kw, jahr) für alle Wochen des Folgezyklus."""
    cycle, target_year = next_cycle(kw, year)
    return [(w, target_year) for w in weeks_in_cycle(cycle, target_year)]


def week_index(kw, year):
    """Monoton steigender Wochenindex, damit sich Abstände über Jahresgrenzen rechnen lassen."""
    try:
        monday = date.fromisocalendar(year, kw, 1)
    except ValueError:
        # KW 53 in einem Jahr, das keine hat -> auf die letzte echte Woche klemmen
        monday = date.fromisocalendar(year, iso_weeks_in_year(year), 1)
    return monday.toordinal() // 7


def weeks_between(kw_a, year_a, kw_b, year_b):
    """Wie viele Wochen liegen zwischen zwei (kw, jahr)-Paaren (b - a)."""
    return week_index(kw_b, year_b) - week_index(kw_a, year_a)
