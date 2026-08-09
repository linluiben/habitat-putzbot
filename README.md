# 🧹 Putzbot

Automatisiert den Putzplan des [Habitat](https://das-habitat.de): lost die wöchentliche Putzcrew aus, trägt sie in Notion ein und benachrichtigt sie per Slack.

Läuft unbeaufsichtigt über GitHub Actions — es gibt keinen Server.

## Wie es funktioniert

Das Jahr ist in **13 Zyklen à 4 Wochen** geteilt (Zyklus 1 = KW 1–4, …). In der letzten Woche eines Zyklus plant der Bot den **kompletten nächsten Zyklus** — vier Wochen Vorlauf, damit die Ausgelosten den Termin einplanen können.

Die Auslosung ist gestaffelt und wird erst gelockert, wenn zu wenige Kandidat:innen übrig sind:

1. Wer selten (≤1×) und lange nicht mehr geputzt hat
2. … dann ohne Rücksicht auf die Schonfrist
3. … dann mit höherer Einsatzzahl (bis maximal 3)

Wer eine Stufe früher qualifiziert war, behält seinen Platz. Angestrebt werden pro Woche 2 neue und 2 länger dabei seiende Mitglieder — damit man beim Putzen auch mal jemanden kennenlernt.

Ausgeloste bekommen eine DM, auf der ✅ und ❌ schon stehen — ein Klick genügt. Bei ❌ fragt der Bot nach der Wunschwoche und trägt um.

## Die Abläufe

| Workflow | Wann | Was |
|---|---|---|
| [`monday_cleanup.yml`](.github/workflows/monday_cleanup.yml) | montags 08:00 UTC | Erinnerung an die aktuelle Woche; am Zyklusende zusätzlich Planung des Folgezyklus. ⚠️ Läuft bis einschließlich KW 36 (31.08.2026) übergangsweise nur im `draw`-Modus |
| [`poll_reactions.yml`](.github/workflows/poll_reactions.yml) | 5× täglich | schaut nach ✅/❌ auf den Auslos-DMs und wickelt Tauschwünsche ab |
| [`sandbox_test.yml`](.github/workflows/sandbox_test.yml) | nur manuell | Testlauf gegen Sandbox-Slack und Notion-Testkopie |
| [`check_tags.yml`](.github/workflows/check_tags.yml) | nur manuell | prüft gegen den echten Workspace, ob jedes Mitglied per E-Mail auffindbar ist |
| [`prod_dm_test.yml`](.github/workflows/prod_dm_test.yml) | nur manuell | echter Slack, Notion-Testkopie, keine Kanal-Nachrichten — für Wege, die sich im Sandbox nicht nachstellen lassen |

Alle lassen sich in der Actions-Oberfläche manuell starten, inklusive Dry-Run- und Debug-Schalter.

## Betriebsarten

```bash
python main.py          # wöchentlich: Erinnerung, am Zyklusende zusätzlich Planung
python main.py poll     # nach ✅/❌-Reaktionen schauen und Tauschwünsche abwickeln
python main.py draw     # nur die laufende Woche auslosen, eine Kanalnachricht, keine DMs
python main.py plan     # nur den Folgezyklus planen (mit DMs), ohne Wochenerinnerung
python main.py tags     # Diagnose: findet Slack jedes losbare Mitglied? Bericht als DM
```

`draw` und `plan` sind die beiden Hälften von `weekly`, einzeln auslösbar. Gedacht für den
Umstieg von V2 auf V3: `draw` ist das alte Verfahren (eine Woche, eine Nachricht) auf der
neuen Auslosungslogik, `plan` erlaubt es, die Zyklusplanung an einem anderen Tag als die
Wochenerinnerung laufen zu lassen.

## Lokal ausführen

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt
cp .env.example .env    # ausfüllen
python main.py
```

Nützlichste Kombination zum Ausprobieren — kompletter Ablauf, aber nichts wird geschrieben oder verschickt:

```bash
DRY_RUN=true DEBUG=true FORCE_PLAN=true python main.py
```

Tests (ohne Netz und ohne Zugangsdaten):

```bash
python tests.py
```

## Wo was steht

Alles außer dieser Datei und `CLAUDE.md` liegt in [`docs/`](docs). `CLAUDE.md` bleibt
bewusst im Wurzelverzeichnis: Claude Code lädt sie nur von dort automatisch für das
ganze Projekt — in einem Unterordner gälte sie nur für diesen Ordner.

| Datei | Inhalt |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Technische Übersicht: Module, Auslosungsregeln, Stolperfallen |
| [implementation-plan.md](docs/implementation-plan.md) | Was gebaut ist, was noch offen ist, und **warum** die Regeln so sind |
| [roadmap.md](docs/roadmap.md) | Ursprünglicher Entwurf + Ausblick (Buttons, Slash-Commands, …) |
| [sandbox-setup.md](docs/sandbox-setup.md) | Testen ohne echte Mitglieder zu behelligen |
| [webhook-setup.md](docs/webhook-setup.md) | Referenz für die Webhook-Variante (aktuell nicht genutzt) |

## Stand

V3 ist gebaut und im Sandbox-Workspace end-to-end getestet (01.08.2026): Mehrwochen-Planung, faire gestaffelte Auslosung, wöchentliche Erinnerung und Tausch per Reaktion — inklusive Umtragen, Nachlosen und dem Nachweis, dass eine einmal verarbeitete Reaktion nicht erneut greift. Einzelheiten in [sandbox-setup.md](docs/sandbox-setup.md).

Der Umstieg auf die Produktivdaten läuft gerade, gestreckt über den August: bis einschließlich KW 36 (31.08.2026) lost der Bot montags nur die laufende Woche aus und postet eine Kanalnachricht — keine DMs. Der volle Prozess mit Zyklusplanung und Tausch per ❌ startet im September mit Zyklus 10 (KW 37–40). Der Fahrplan samt Begründung steht in [implementation-plan.md](docs/implementation-plan.md).

Später geplant: Umzug auf den Hetzner-Server mit Socket Mode, dann Reaktionen in Sekunden statt Stunden und darauf aufbauend Buttons statt Emoji-Reaktionen.
