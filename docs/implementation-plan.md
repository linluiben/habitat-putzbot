# Implementation-Plan: Putzbot V3

Reihenfolge/Checkliste, um von der heutigen Single-File-V2 zum in [roadmap.md](roadmap.md) beschriebenen Mehrwochen-Zyklus mit Reschedule-Flow zu kommen. Jede Phase soll für sich funktionsfähig/deploybar sein — nicht alles auf einmal umbauen.

## Geklärte Entscheidungen

Stand der Abstimmung, gilt als verbindlich für die Implementierung:

| Thema | Entscheidung |
|---|---|
| Zyklus-Anker | Zyklus 1 = KW 1–4, … Zyklus 13 = KW 49–52. KW 53 (in manchen ISO-Jahren) zählt zu Zyklus 13, eröffnet keinen neuen. |
| Jahres-Eindeutigkeit | Neue Property **`Jahr` (number)** in der Putzplan-DB. Bot schreibt sie beim Anlegen, alle Lookups filtern auf `Kalenderwoche` + `Jahr`. |
| `Putzstatus` (Mitgliederliste) | Losbar ist **nur `Normal`**. `Ausgetragen`, `Neu`, `Priorität` und `Postponed` fliegen raus — **und leer ebenfalls** (Stand 02.08.): ein leerer Status heißt, dass über die Person noch niemand entschieden hat, und unentschieden ist kein Ja. Wer mitmachen soll, bekommt `Normal`. (Sonderbehandlung von `Priorität`/`Postponed` evtl. später.) |
| ❓-Icon | Entfällt — wird durch `Putzstatus` ersetzt. |
| `min_size` | Entfällt. Alles läuft über `needed` = Zielgröße (4) − bereits für die Woche eingetragene Mitglieder. |
| Putzhäufigkeit | Gestaffelt: zuerst nur Mitglieder mit ≤1 Putzeinsatz, dann ≤2, dann ≤3. **Obergrenze 3.** |
| Aktualität | Wer in den letzten 3 Zyklen (12 Wochen) geputzt hat, kommt erst in den Fallback-Stufen in den Topf. |
| Alt/Neu | „Neu" = Eintrittsdatum < 1 Jahr her. Ziel 2 neue + 2 alte pro Woche, aber **weichstes Kriterium** — wird in `raffle.py` behandelt, nicht im Pool-Bau. |
| Fallback-Fairness | Wer **vor** einer Fallback-Lockerung schon im Topf war, wird garantiert gezogen; nur die Restplätze werden mit gelockerten Kriterien besetzt. |
| Reschedule-Kapazität | **Harte Grenze bei 4.** Zielwoche mit weniger als 4 Leuten = ok, sonst abgelehnt mit Link zur Woche und Bitte, eine andere zu wählen. Der zwischenzeitlich angedachte weiche Puffer (5 = Crew fragen) ist wieder raus — viel Mechanik für einen Fall, der kaum eintritt, weil immer nur ein Zyklus im Voraus geplant wird und weiter entfernte Wochen praktisch leer sind. Steht als Idee im Feature-Tracker. |
| Reschedule-Lookup | Läuft immer `Notion Lookup` für alte + neue Woche (auch um die Belegung zu prüfen und die Seite ggf. anzulegen). |
| Reaktionen | Ausgeloste Mitglieder stehen **von Anfang an** in ihrer Woche. Nur ❌ trägt sie wieder aus. |
| Erinnerungen/Fristen | Kein Auto-Confirm, keine Deadline in V3. Vertagt auf **V3.1** (Feature-Tracker-DB `2f6b71ac7d098024af8bd0059351cd87`). |
| Testing | Erst Sandbox-Slack-Workspace aufsetzen, dann dort testen. Kein Live-Test mit echten Mitgliedern vorher. |

## Reale Notion-Schemas (per Connector verifiziert)

**Mitgliederliste** — `collection://32b442c0-9a5c-4666-9f78-6647909752b8` (= `DS_A_ID`)
`Name, Vorname` (title, Format „Nachname, Vorname") · `Eintrittsdatum` (date) · `Austrittsdatum` (date) · `E-Mail`, `Interne Email` (email) · `Mitgliedsstatus` (multi_select) · `Onboarding: Status` (select) · **`Putzstatus`** (select: `Ausgetragen`/`Priorität`/`Postponed`/`Normal`/`Neu`) · `Putzplan` (relation → Putzplan) · `Putzanzahl` (rollup, count)

**Putzplan** — `collection://2eab71ac-7d09-8055-9bf6-000bb4351efb` (= `DS_B_ID`)
`Titel` (title) · `Kalenderwoche` (number) · `Mitglieder` (relation → Mitgliederliste) · `Anzahl Mitglieder` (rollup) · `Status` (status: `Geplant`/`Crew voll`/`Nicht auswählen`/`Erledigt`) · `Archiv` (checkbox) · **kein Jahr** (→ wird ergänzt, s.o.)
Templates: „Neue Putzcrew (Automation)" `2eab71ac-7d09-80ef-954f-d3e298915dfe` · „Putzcrew KW " (default) `2f3b71ac-7d09-8090-a23b-d382f6fa64d5`

### Dabei gefundene Bugs in V2

- **Kein Jahresfilter** beim KW-Lookup → im Januar kann die Seite des Vorjahres mit derselben KW getroffen werden. Wird durch die `Jahr`-Property behoben.
- **Keine Pagination** bei den Notion-Queries (`has_more`/`next_cursor` werden ignoriert). Bei ~80 Mitgliedern geht das noch gut, ab 100 werden Mitglieder stillschweigend übersehen.
- **`Archiv` und `Status: Nicht auswählen` werden ignoriert** — archivierte bzw. bewusst gesperrte Wochen können getroffen/befüllt werden.

## Phase 0 — Vorbereitung (manuell, blockiert Phase 2+)

- [ ] Property **`Jahr` (number)** in der Putzplan-DB anlegen.
- [ ] Bestehende Putzplan-Seiten mit `Jahr` backfillen (alle bisherigen dürften 2026 sein).
- [ ] Sandbox-Slack-Workspace aufsetzen (siehe [sandbox-setup.md](sandbox-setup.md)).
- [ ] Prüfen, ob `TEMPLATE_ID` auf „Neue Putzcrew (Automation)" zeigt.

## Phase 1 — Refactor zu Mehrdatei-Struktur ✅

- [x] `config.py`, `cycles.py`, `notion.py`, `raffle.py`, `slack_utils.py`, `scheduler.py` aus [main.py](../main.py) herausgezogen.
- [x] `main.py` orchestriert nur noch.
- [x] `monday_cleanup.yml` behält `python main.py` als Einstiegspunkt.
- [x] Pagination in allen Notion-Queries ergänzt.
- [x] `DRY_RUN` überarbeitet (läuft jetzt den **ganzen** Flow durch und überspringt nur die Schreibzugriffe) + neues `DEBUG` für Detail-Diagnostik.

## Phase 2 — Candidate-Pool & Raffle ✅

- [x] `Putzstatus`-Filter ersetzt den ❓-Icon-Check.
- [x] Gestaffelte Pool-Bildung (≤1/≤2/≤3 Einsätze × Aktualität) mit Locking der jeweils strengeren Stufe.
- [x] Alt/Neu-Balance (2+2) in `raffle.py`, unter Berücksichtigung der schon eingetragenen Mitglieder.

## Phase 3 — `scheduler.py` (Plan-Prozess) ✅

- [x] Zyklus-Mathematik inkl. KW 53 und Jahreswechsel (`cycles.py`).
- [x] Plan-Lauf in der letzten Woche eines Zyklus: Schleife über die 4 Wochen des Folgezyklus, Seite anlegen falls nötig, `Raffle` falls unterbesetzt.
- [x] Wochen mit `Status: Nicht auswählen` oder `Archiv: true` werden übersprungen.
- [x] DMs an neu ausgeloste Mitglieder (❌-Hinweis erst aktiv, wenn `RESCHEDULE_ENABLED`).

## Phase 4 — Remind-Prozess ✅

- [x] Wöchentliche Erinnerung mit @-Erwähnungen der Crew in den Zielkanal.
- [x] Läuft jeden Montag, unabhängig vom Plan-Lauf.

## Phase 5 — Reaktionen per Polling (statt Webhook) ✅

**Entscheidung:** Kein Webhook-Server für V3. Der Bot fragt stattdessen mehrmals täglich bei Slack nach, ob jemand auf seine DMs reagiert hat. Begründung: Ausgelost wird 4 Wochen im Voraus, ein paar Stunden Reaktionszeit sind völlig ausreichend — und dafür entfallen Server, öffentliche URL, HTTPS und Hosting komplett. Läuft weiter auf GitHub Actions.

Der Umzug auf Socket Mode (dauerhafte WebSocket-Verbindung, Reaktionen in Sekunden) kommt mit dem Hetzner-Server, siehe Phase 9. Der Zustands-/Zuordnungsteil ist bei beiden Varianten identisch, der Wechsel betrifft nur die Zustellung.

- [x] Zuordnung DM ↔ (Mitglied, KW, Jahr) über **Slack-Message-Metadata** — kein externer Speicher nötig, die Info hängt an der Nachricht selbst.
- [x] Eigener Workflow `poll_reactions.yml`, 5× täglich zu Wachzeiten. Beide Workflows teilen sich eine `concurrency`-Group, damit nie zwei Läufe gleichzeitig schreiben.
- [x] `python main.py poll` als eigener Modus neben dem wöchentlichen Lauf.

## Phase 6 — `reschedule.py` ✅

- [x] ❌ auf der Auslos-DM → Bot fragt per DM nach der Zielwoche (Zahleneingabe).
- [x] Antwort des Mitglieds aus der DM-Historie lesen und validieren (existierende KW, in der Zukunft, innerhalb der nächsten 10 Zyklen, nicht gesperrt).
- [x] **Erst bei gültiger Antwort** wird umgetragen — bis dahin bleibt das Mitglied in seiner Woche, damit sie nicht unbesetzt dasteht, falls nie eine Antwort kommt (so steht es auch in [roadmap.md](roadmap.md)).
- [x] Kapazitätsprüfung der Zielwoche: weniger als 4 Leute = umtragen, sonst ablehnen und mit Link zur Woche erneut fragen.
- [x] Zielwoche anlegen, falls es noch keine Seite gibt.
- [x] Falls die alte Woche dadurch unterbesetzt ist: erneut auslosen, mit Ausschluss des gerade Ausgetragenen.
- [x] `RESCHEDULE_ENABLED = True` (schaltet den ❌-Hinweis in den DMs frei).

Noch nicht live verifiziert: der komplette Reaktions-Durchlauf (dafür fehlen die
DM-Scopes der Sandbox-App, siehe [sandbox-setup.md](sandbox-setup.md)). Die
Entscheidungslogik ist offline getestet.

## Phase 7 — End-to-End-Test im Sandbox-Workspace

Läuft über `.github/workflows/sandbox_test.yml` (nur manuell, `SANDBOX=true` und
`USE_TEST_DATA=true` fest verdrahtet statt als Input — der Workflow soll die
Produktivdaten strukturell nicht erreichen können). Braucht von den
Produktiv-Secrets nur `NOTION_TOKEN` und `DS_A_ID`.

- [x] Übergangsmodi `draw` und `plan`, damit Wochenauslosung und Zyklusplanung
      an verschiedenen Tagen laufen können (siehe Umstiegsplan unten).
- [x] DM-Zuordnung pro Mitglied (`reschedule.verlauf_fuer`): mit
      `SLACK_TEST_USER_ID` landen alle DMs im selben Kanal, ohne den Filter
      hätte ein einzelnes ❌ die ganze Wochencrew umgetragen.
- [x] **Kompletter Durchlauf am 01.08.2026 live bestanden:** Plan → Raffle → DM →
      ❌ → Nachfrage → volle Zielwoche abgelehnt → gültige Antwort → Umtragen →
      Zielwoche angelegt → Nachlosen → Bestätigung.
- [x] **Slack liefert die Message-Metadata über `conversations_history` zurück** —
      30 von 30 Nachrichten. Das war die letzte unverifizierte Annahme des
      Reschedule-Flows. `read_dm_history` gibt das bei `DEBUG=true` als Zeile
      „N Nachrichten, M vom Bot, K mit Metadata" aus.
- [x] Zielwoche voll → Ablehnung mit Notion-Link und erneuter Nachfrage, ohne
      dass irgendetwas umgetragen wird.
- [x] Idempotenz: zweiter Poll direkt danach meldet „Nichts Neues."
- [x] Der Mitglieds-Filter greift live: 16 Mitglieder lasen denselben DM-Verlauf
      (Sandbox-Umleitung), nur der tatsächlich Betroffene löste den Tausch aus.
- [x] ✅ auf eine Auslos-DM löst nichts aus — im selben Poll wie ein ❌ eines
      anderen Mitglieds geprüft, es wurde nur das ❌ bearbeitet.
- [x] Freitext-Antwort („ich würde gerne in KW 41 putzen, geht das?") und
      Unsinn-Antwort verhalten sich wie vorgesehen.
- [x] **Gegen den echten Slack-Workspace** (Workflow `prod_dm_test.yml`, Notion
      weiterhin auf der Testkopie, `DM_ONLY=true`): gesperrte Zielwoche wird mit
      „diese Woche ist gesperrt" abgelehnt, eine Woche außerhalb der Reichweite
      mit „mehr als 10 Zyklen im Voraus". In beiden Fällen wird nichts umgetragen
      und erneut nachgefragt.
- [ ] Live nicht durchgespielt: zu wenige Kandidaten, Jahreswechsel (KW 52 → KW 1),
      KW-53-Jahr. Offline in [tests.py](../tests.py) abgedeckt.

### Dabei gefundene Eigenheit: „Vergangenheit" ist unerreichbar

`zielwoche_bestimmen` bildet eine KW ≤ der aktuellen immer aufs **Folgejahr** ab.
Der Abstand ist dadurch nie ≤ 0, und der Zweig „die Woche liegt schon in der
Vergangenheit" kann nicht eintreten. Wer „20" antwortet, bekommt stattdessen
„mehr als 10 Zyklen im Voraus" — inhaltlich richtig, aber die
Vergangenheits-Meldung ist toter Code. Live bestätigt am 01.08.

## Umstiegsplan V2 → V3 (KW 32–37, 2026)

Der Wechsel läuft über mehrere Wochen statt an einem Tag. Grund: die ersten beiden
Augustwochen fallen in den Urlaub, und ein unbeaufsichtigt klemmender DM-Flow wäre
der schlechteste Einstand. Bis Ende August wird deshalb nur ausgelost, der volle
Prozess startet im September.

`monday_cleanup.yml` steht dafür planmäßig auf `draw` statt `weekly` (der Cron läuft
also ganz normal weiter, nur in einem anderen Modus).

| Wann | Modus | Was passiert |
|---|---|---|
| Mo, KW 32 (03.08.) | `draw`, manuell | KW-32-Seite anlegen, auf 4 auffüllen, **eine** Kanalnachricht |
| Mo, KW 33–36 | `draw`, per Cron | jede Woche dasselbe: auffüllen + eine Nachricht, keine DMs |
| in KW 36 (ab 31.08.) | `plan`, manuell | Zyklus 10 (KW 37–40) nach dem neuen Verfahren, **mit** DMs und Reschedule |
| davor, von Hand | Ankündigung | Was sich mit V3 ändert (Zyklusplanung, DMs, Tausch per ❌) |
| ab KW 37 (07.09.) | `weekly` | Workflow zurückstellen, Normalbetrieb |

Zwei Fallstricke, die den Fahrplan bestimmen:

1. **`weekly` lost nie aus.** Es ruft nur `remind_current_week` auf, das an bereits
   Eingetragene erinnert. Ohne geplanten Zyklus existiert für die Woche gar keine
   Notion-Seite, und dann steigt die Erinnerung vorzeitig aus („keine Notion-Seite —
   keine Erinnerung verschickt"). Ein Montag im `weekly`-Modus ohne vorherige
   Zyklusplanung bleibt also komplett stumm und niemand putzt.
2. **KW 36 ist Zyklusende.** Stünde der Workflow dort schon auf `weekly`, würde zwar
   Zyklus 10 geplant, KW 36 selbst bliebe aber unbesetzt. Deshalb läuft `draw` eine
   Woche länger als der Urlaub dauert, und die Planung wird einmalig von Hand
   angestoßen (Workflow mit `modus = plan` starten).

`poll_reactions.yml` bleibt die ganze Zeit an. Solange es keine Auslos-DMs gibt,
findet der Lauf keine zukünftigen Wochen und meldet „Nichts zu prüfen" — er ist also
wirkungslos. Abschalten würde nur das Risiko schaffen, das Wiedereinschalten beim
`plan`-Lauf zu vergessen; dann liefen alle ❌-Reaktionen unbemerkt ins Leere.

## Phase 7.1 — Reaktionen vorsetzen ✅

- [x] Der Bot setzt ✅/❌ auf seiner Auslos-DM selbst vor (`PREFILL_REACTIONS`), damit
      das Mitglied nur noch klicken muss. Ein unbekanntes Emoji löst nämlich
      **stillschweigend nichts** aus — die Person hält die Sache trotzdem für erledigt.
- [x] `read_dm_history` filtert die eigenen Reaktionen wieder heraus
      (`eigene_user_id()` via `auth.test`). Ohne das sähe jede Auslos-DM wie ein ❌
      aus und der Poll fragte die komplette Crew.
- [x] Am 01.08. gegen den echten Workspace verifiziert: DM trägt beide Emojis,
      Poll ohne Klick meldet „Nichts Neues", nach dem Klick kommt die Nachfrage.
- [x] Braucht `reactions:write` — **produktiv fehlte dabei sogar `reactions:read`**,
      der Poll hätte dort also nie eine Reaktion gesehen.

## Phase 8 — Cutover

- [ ] **`python main.py tags` gegen den echten Workspace** (Workflow
      `check_tags.yml`). Prüft für jedes losbare Mitglied, ob sich die Slack-ID
      über die E-Mail finden lässt, und schickt das Ergebnis als DM an
      `SLACK_TEST_USER_ID`. Im Sandbox nicht testbar, weil dort die echten
      Adressen nicht existieren. Ergebnis abarbeiten, dann `Interne Email` in
      Notion für alle Fehlenden ausfüllen — eine abgeleitete Adresse ist geraten
      und scheitert lautlos.
- [ ] Vorab einmal `DRY_RUN=true DEBUG=true` gegen den **echten** Slack-Workspace laufen lassen und die fertigen Nachrichtentexte prüfen.
- [ ] Auf echten Workspace/Kanal + echte Notion-IDs umstellen.
- [ ] Sammelseiten „Ausgetragen" (KW 0) und „Postponed" (KW 54) in Notion löschen, jetzt wo `Putzstatus` sie ersetzt.
- [ ] Ersten vollen Zyklus eng beobachten.

## Phase 9 — Umzug auf Hetzner + Socket Mode

- [ ] Socket Mode statt Polling: dauerhafte WebSocket-Verbindung, Reaktionen in Sekunden statt Stunden. Braucht ein App-Level-Token (`xapp-…`) und einen dauerhaft laufenden Prozess — aber **keine** öffentliche URL, kein HTTPS, keinen Reverse Proxy.
- [ ] Der Zuordnungsteil aus Phase 5/6 bleibt unverändert; nur die Zustellung wechselt.
- [ ] Danach sind Buttons und Slash-Commands möglich (V3.2/V3.3, siehe [roadmap.md](roadmap.md)) — die gehen per Polling grundsätzlich nicht.
- [ ] [webhook-setup.md](webhook-setup.md) beschreibt die HTTP-Webhook-Variante. Die ist für diesen Fall vermutlich nicht mehr nötig; das Dokument bleibt als Referenz.

## Idee (nicht gebaut): erfassen, wer tatsächlich geputzt hat

**Das Problem:** `putz_count` zählt **Zuteilungen, keine Erledigungen.** Wer eingetragen
war und nicht kam, ist im System nicht von jemandem zu unterscheiden, der da war — und
wird für die Fairness-Rechnung sogar genauso behandelt („hat ja letztens erst"). Der
Wochenstatus `Erledigt` existiert in Notion, wird aber von nichts gesetzt; `setze_status`
überschreibt ihn bewusst nie. Im Channel steht dazu schon eine Beschwerde („niemand hat
abgesagt, nur ich war da").

**Skizze der naheliegendsten Variante.** Der Montagslauf fragt die Crew der *vergangenen*
Woche per DM: „Hast du geputzt?", mit vorgesetztem ✅/❌ wie bei der Auslosung. Wer ✅
klickt, landet in einer neuen Relation `Bestätigt` auf der Wochenseite; ist die Woche
durch, geht sie auf `Status: Erledigt`.

Der Reiz daran ist, dass praktisch nichts Neues nötig wäre: Message-Metadata, vorgesetzte
Reaktionen, der Poll-Lauf und der Mitglieds-Filter aus Phase 5–7.1 tragen das
unverändert. Dazu kämen im Wesentlichen ein Modus, ein Metadata-Typ und eine
Notion-Property.

**Was dagegen spricht**, und weshalb es bewusst noch nicht gebaut ist:

- Es bleibt **Selbstauskunft**. Wer nicht putzt, klickt eher gar nicht als ❌ — und „keine
  Reaktion" ist nicht dasselbe wie „nicht geputzt".
- Eine DM mehr pro Person pro Einsatz. Aufmerksamkeit ist die knappe Ressource; wenn der
  Bot zu oft fragt, reagiert irgendwann niemand mehr — auch nicht auf das Wichtige.
- Die ausgehängte Liste bleibt ohnehin die belastbarere Quelle. Das hier wäre eine
  Ergänzung, kein Ersatz.

Sinnvoll erst zu entscheiden, wenn der normale Ablauf ein paar Zyklen gelaufen ist und
sich zeigt, ob das Problem in der Praxis groß genug ist.

## Priorität, falls Zeit knapp ist

Phasen 1–4 liefern schon eigenständigen Mehrwert: korrekter Mehrwochen-Zyklus, fairere Auslosung, wöchentliche Erinnerung — nur ohne Reschedule. Phasen 5–6 sind mit dem Polling-Ansatz deutlich kleiner geworden und brauchen keine neue Infrastruktur.
