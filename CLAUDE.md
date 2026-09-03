# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Putzbot" — a scheduled bot that runs the cleaning-crew ("Putzplan") lottery for a club (das-habitat.de). It reads member and cleaning-schedule data from two linked Notion data sources, draws members to fill upcoming weeks' crews, writes the result back to Notion, and notifies people via Slack. It runs unattended via GitHub Actions (`.github/workflows/monday_cleanup.yml`) on a cron (Mondays 08:00 UTC) or manually via `workflow_dispatch`.

Two scheduled workflows drive production (`monday_cleanup.yml`, `poll_reactions.yml`).

⚠️ **`monday_cleanup.yml` is temporarily running `draw` instead of `weekly`** — through KW 36 (2026-08-31), during the V2→V3 changeover. The mode is a literal in the `run:` line, marked `⇦`, alongside the `modus` dispatch input. Both revert to `weekly` from KW 37. Do not "fix" this back without reading the Umstiegsplan in [implementation-plan.md](docs/implementation-plan.md): `weekly` never draws anybody, so switching early leaves weeks with no crew and no message at all.

Three more workflows are **manual-only and exist for testing**; each hardcodes its safety switches rather than exposing them as inputs, so a misclick cannot reach production:

| Workflow | Slack | Notion | Guard |
|---|---|---|---|
| `sandbox_test.yml` | sandbox | test copy | `SANDBOX=true` + `USE_TEST_DATA=true` hardcoded. Needs only `NOTION_TOKEN` and `DS_A_ID` from the production secrets, because `config.py` swaps in the `SANDBOX_*`/`TEST_*` values. |
| `prod_dm_test.yml` | **real** | test copy | `DM_ONLY=true` hardcoded, plus a step that aborts when `SLACK_TEST_USER_ID` is empty — without the redirect, DMs would reach real members. For paths the sandbox cannot reproduce. |
| `check_tags.yml` | **real** | read-only | The `tags` mode touches no write path and aborts without `SLACK_TEST_USER_ID`. Must run against the real workspace: in the sandbox none of the real addresses exist. |

The year is divided into **13 cycles of 4 weeks** (cycle 1 = KW 1–4, … cycle 13 = KW 49–52; in 53-week ISO years KW 53 joins cycle 13). Every Monday the bot posts a reminder for the current week; in the **last week of a cycle** it additionally plans the whole *next* cycle — creating pages and drawing crews four weeks in advance so people can plan around it.

[roadmap.md](docs/roadmap.md) is the design doc for the full target state; [implementation-plan.md](docs/implementation-plan.md) tracks which parts are built and records the agreed-upon decisions (candidate-pool rules, reschedule thresholds, verified Notion schemas). **Read implementation-plan.md before changing raffle or scheduling behavior** — it documents *why* the rules are what they are. The reschedule flow is built and verified against the real workspace; it runs on **polling**, not on a webhook. [webhook-setup.md](docs/webhook-setup.md) describes the HTTP variant that was considered and dropped — kept as reference, not as a description of the current state.

Everything except `README.md` and this file lives in [`docs/`](docs). `CLAUDE.md` stays at the repo root on purpose: Claude Code only picks it up project-wide from there, and inside a subfolder it would apply to that folder alone.

## Commands

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt
```

Run the bot (requires the env vars below):
```bash
python main.py          # weekly: reminder, plus next-cycle planning at cycle end
python main.py poll     # check for ✅/❌ reactions and handle swap requests
python main.py draw     # transition mode: fill the CURRENT week, one channel
                        # message, no DMs, no cycle planning
python main.py plan     # plan the next cycle only (with DMs), no reminder
python main.py tags     # diagnostic: can every eligible member be found in
                        # Slack by email? Report comes as a DM, writes nothing
python main.py erledigt # reconcile who actually cleaned: read the :broom:
                        # reactions on past weeks' channel messages, ask the
                        # silent ones by DM, and close weeks whose deadline
                        # has passed
```

`draw` and `plan` exist for the V2→V3 changeover: `draw` is the old one-week-at-a-time
procedure running on the new raffle logic, and `plan` lets the cycle planning happen on a
different day than the weekly reminder. `weekly` is unchanged and still does both.

Run the offline test suite — no credentials or network needed, also runs in CI:
```bash
python tests.py
```

There is no test framework; `tests.py` is a plain script that fakes the Notion/Slack layer and exits non-zero on failure. It covers what is painful to test live: year boundaries, 53-week years, exhausted candidate pools, and double-booking within a cycle. Add cases there rather than writing throwaway scripts.

## Environment variables

| Variable | Purpose |
|---|---|
| `NOTION_TOKEN` | Notion integration token |
| `SLACK_TOKEN` | Slack bot token |
| `DS_A_ID` | Notion data source: Mitgliederliste |
| `DS_B_ID` | Notion data source: Putzplan |
| `SLACK_CHANNEL_ID` | Channel for reminders and cycle summaries |
| `TEMPLATE_ID` | Notion page template for a new week's page |
| `DRY_RUN` | `"true"` → run the whole flow but skip every Notion/Slack write |
| `DM_ONLY` | `"true"` → suppress channel messages, still send DMs. For tests against the **real** workspace, where a stray post to #räumen-und-ratschen is the one thing you cannot take back. Enforced inside `post_channel`, like `DRY_RUN`. |
| `DEBUG` | `"true"` → verbose diagnostics (per-tier candidate counts, lookups) |
| `FORCE_PLAN` | `"true"` → run cycle planning even outside the last week of a cycle |
| `SLACK_TEST_USER_ID` | If set, **all** DMs are redirected to this user (sandbox testing — see [sandbox-setup.md](docs/sandbox-setup.md)) |
| `SANDBOX` | `"true"` → switch Slack to the sandbox workspace |
| `SANDBOX_SLACK_TOKEN`, `SANDBOX_SLACK_CHANNEL_ID` | Required when `SANDBOX=true`; config aborts rather than falling back to the real workspace |
| `SANDBOX_SLACK_TEST_USER_ID` | Sandbox DM target — a *different* user ID than in the real workspace |
| `USE_TEST_DATA` | `"true"` → use the Notion test copies instead of production |
| `TEST_DS_B_ID`, `TEST_TEMPLATE_ID` | Required when `USE_TEST_DATA=true`; config aborts rather than silently falling back to production |
| `TEST_DS_A_ID` | Optional — the Mitgliederliste is only ever read, so the real one is fine for tests |
| `PUTZPLAN_RELATION_PROP` | Name of the Mitgliederliste→Putzplan relation (default `Putzplan`). A duplicated Putzplan adds a *second* relation property (`TEST_PUTZPLAN_RELATION_PROP`); point this at it when testing. |

In production these come from GitHub Actions secrets. Locally, copy [.env.example](.env.example) to `.env` — `config.py` loads it via a small built-in parser (no dependency). Real environment variables always win over the file, so CI is unaffected.

## Module layout

| File | Responsibility |
|---|---|
| [main.py](main.py) | Entrypoint. Orchestration only — decides reminder vs. reminder + planning. |
| [config.py](config.py) | Env vars, Slack client, Notion headers, and **all tunable rules** as constants. |
| [cycles.py](cycles.py) | Pure date math: cycle ↔ week mapping, year boundaries, week distances. No I/O. |
| [notion.py](notion.py) | Every Notion call: paginated queries, week lookup, member loading, writes. |
| [raffle.py](raffle.py) | Candidate-pool tiers and the draw itself. |
| [slack_utils.py](slack_utils.py) | User lookup, sending, and all message texts. |
| [scheduler.py](scheduler.py) | Scheduled processes (`remind_current_week`, `plan_next_cycle`) plus `fill_week`, the shared draw-write-notify step. |
| [reschedule.py](reschedule.py) | Poll for ✅/❌ reactions and move members between weeks. |
| [tagcheck.py](tagcheck.py) | One-off diagnostic for the cutover: does every eligible member resolve to a Slack ID? |
| [tracking.py](tracking.py) | Erledigt-Tracking: reconcile assignment against who actually cleaned, and write the result back. |

`cycles.py` is separate from `scheduler.py` because both `raffle.py` and `scheduler.py` need week math; folding it in would create an import cycle.

`SANDBOX` and `USE_TEST_DATA` are deliberately two switches (Slack vs. Notion) rather than one "test mode": dry-running the raffle against *real* Notion data while pointing Slack at the sandbox is the most common combination. Both abort loudly if their required companions are missing — never a silent fallback to production.

`DRY_RUN` is enforced *inside* the write functions in `notion.py`/`slack_utils.py`, so callers never check it. Any new write must respect this, or dry runs silently stop being safe.

## How the draw works

Members are found in Slack **only** by email — `Interne Email` if set, otherwise derived from `Nachname, Vorname`. A derived address is a guess, and a wrong guess is silent: that person simply never gets a DM. `notion._load_members` records which of the two it was in `member["email_quelle"]`, and `python main.py tags` turns that into a report. Run it against the **real** workspace before the cutover — the sandbox cannot test it, because the real addresses do not exist there.

Eligibility is filtered in the Notion query itself (`notion.MEMBER_FILTER`): active membership status, onboarding done, and `Putzstatus` exactly `Normal` (`config.PUTZSTATUS_ELIGIBLE`). `Ausgetragen`, `Neu`, `Priorität` and `Postponed` are excluded — **and so is an empty `Putzstatus`**: empty means nobody has decided about that person yet, and undecided does not mean opted in. Whoever should take part gets `Normal`. (The V2 "❓ page icon" check is gone — `Putzstatus` replaced it.)

`notion.TAG_CHECK_FILTER` is deliberately wider on that one property: it excludes only `Ausgetragen`, so `python main.py tags` also checks people who are `Neu` or have no status yet. Someone who is `Neu` today is `Normal` in two months, and by then their email should already be verified rather than failing on their first assignment. Everything else (membership, onboarding, exit date) is identical in both filters — there is a regression test for exactly that split.

⚠️ **Notion filters match option names, not IDs.** Rename an option in Notion and `contains` silently matches nothing — no error, just a quietly smaller pool. This bit once: `Vorläufiges Mitglied` → `Probemitglied` and `passives Mitglied` → `passiv` cut the pool from 63 to 30 *and* let two passive members in, without a single warning. `notion.pruefe_filter_optionen()` now runs at startup, compares every select value used in the filters against the live schema, and aborts with the actual option list if one is gone. `MEMBER_FILTER` is meant to stay in lockstep with the Notion view "Putzen"; if you change one, change the other.

### People who entered themselves in Notion beforehand

Volunteers are handled by four separate pieces that have to agree, so it is worth stating in one place:

- They are **never drawn again**: `raffle.select_crew` starts from `blocked = set(week["member_ids"])`.
- They **count toward the target size**: `fill_week` only draws `needed = CREW_SIZE - week["member_count"]`, so a week with four volunteers draws nobody ("Crew ist schon vollzählig").
- They **count toward the 2-new/2-old mix** via `already_on_page`, and toward future fairness — the Notion relation feeds `putz_count` and `MIN_WEEKS_BETWEEN` like any other assignment.
- They **are mentioned** in reminders and summaries, because `lookup` comes from `get_all_members()` (unfiltered) rather than the eligible list — so someone with `Putzstatus: Ausgetragen` who volunteers anyway still resolves to a name.

The deliberate limit: volunteers get **no DM**, and therefore **no ❌ option** — `fill_week` only messages `selected`. Someone who signed up on their own has decided, and can un-sign in Notion just as easily. Only the `draw` mode distinguishes them in its message ("Danke fürs freiwillige Eintragen" vs. "Ausgelost wurden"); the cycle summary lists everyone together.

### The tier ladder

`raffle.select_crew` then walks a ladder of tiers from strict to loose:

```
≤1 Einsätze + Schonfrist → ≤1 Einsätze → ≤2 + Schonfrist → ≤2 → ≤3 + Schonfrist → ≤3
```

Two rules that are easy to break by accident:

1. **Locking.** If a tier has fewer candidates than open slots, *all* of them are locked in and only the remaining slots fall through to the looser tier. Someone who qualified under strict criteria must not lose their spot because the criteria were relaxed afterwards.
2. **`ist_zu_dicht_dran` is a hard block.** The soft "Schonfrist" (`RECENCY_WEEKS`, 12) is relaxable by design; the minimum gap between two assignments (`MIN_WEEKS_BETWEEN`, 4) is *not*, and applies on every tier. Without it the relaxed tiers happily draw someone for two weeks of the same cycle. There is a regression test for this.

The 2-new/2-old mix (`ist_neu` = joined less than a year ago) is the weakest criterion and only applies when sampling within the final tier, counting members already on the page.

`enrich_members` must be re-run per target week — distances are relative to the week being planned. Crews drawn earlier in the same run are tracked in `member["extra_weeks"]`, because the Notion relation is stale until the run finishes.

## How the Erledigt-Tracking works

`putz_count` used to count **assignments, not completions** — someone who was
assigned and never showed up looked exactly like someone who did, and the
fairness rules even treated them as "just cleaned recently". `tracking.py`
reconciles that once per week.

The confirmation is a **:broom: reaction on the weekly channel message**, not a
DM. The crew is long settled by then and the post is there anyway. A deliberately
unusual emoji: a ✅ or 👍 under a reminder just as plausibly means "seen" or
"good idea", and every misread costs somebody an unwarranted "you weren't even
on duty — shall I add you?" DM. The bot pre-seeds the :broom: itself and the
message text spells out what it means. A ❌ **in the channel** is deliberately
not evaluated — whoever does not react gets the follow-up DM anyway, and there
✅/❌ is unambiguous because the question is right next to it.

Three Mondays, for cleaning week KW N:

| When | What |
|---|---|
| Mon KW N | Week message with metadata `{kw, jahr}` and a pre-seeded :broom:. |
| Mon KW N+1 | **Abgleich**: read the reactions. Assigned members without a reaction, and reactors without an assignment, get a DM with a deadline. Notion is *not* touched yet. |
| Mon KW N+2 | **Abschluss**: deadline is up. Notion is written **once**, status goes to `Erledigt`. Default for everyone who never answered: assigned members are removed, extra reactors are not added. |

**One** relation, by decision: whoever did not clean is removed from `Mitglieder`
on the week page, whoever cleaned extra is added. `putz_count` and the recency
rules correct themselves as a side effect. The price is that "was assigned, did
not clean" is afterwards only visible in Slack and in Notion's page history.

No extra store: the deadline follows from the week distance, "already asked?"
from the presence of the follow-up DM, "already closed?" from the Notion status.

Three guards that matter:

- **`TRACKING_START_KW`/`TRACKING_START_YEAR` and `TRACKING_MAX_LOOKBACK_WEEKS`.**
  Without them the first run would sweep the entire history and remove everyone
  from every old week — those channel messages carry no metadata, so it would
  look like nobody ever cleaned.
- **No channel message found → nobody is removed.** "No reactions" must never be
  read as "nobody cleaned". After the deadline such a week only gets its status
  set, so it stops nagging.
- **A follow-up DM sent in this very run defers the closing to the next one.**
  Otherwise a missed Monday would mean the deadline was zero seconds long.

`slack_utils.slack_id_fuer` honours `SLACK_TEST_USER_ID` exactly like the DMs do:
in the sandbox all members resolve to the test user, so one :broom: from that
user confirms the whole crew. That is the only way to exercise this path there —
the real addresses do not exist in the sandbox.

Reading the channel needs the **`channels:history`** scope (`groups:history` if
the channel ever becomes private) and the bot must be a member of the channel.
The exact reactor list is fetched with `reactions.get`, not taken from
`conversations_history`, because the `users` list there can be truncated — and a
truncated list would read as "did not clean".

## How the reschedule flow works

There is **no webhook server**. `python main.py poll` runs several times a day (`.github/workflows/poll_reactions.yml`) and asks Slack whether anyone reacted. Socket Mode replaces this later (Phase 9); the state handling below is identical either way.

The mapping *DM message → (member, week)* lives in **Slack message metadata** (`chat_postMessage(metadata=…)`, read back via `conversations_history(include_all_metadata=True)`). That is why no external store is needed — requires `slack-sdk>=3.21`.

`reschedule.naechster_zustand` decides what to do from the DM history, and the rule that makes it safe is: **the newest bot message wins, even if it carries no metadata.** A confirmation ("Erledigt, du bist jetzt in KW 22") therefore terminates the scan. Without that, the loop would skip past the untagged confirmation, re-find the older question plus answer, and execute the same move a second time on every poll. There is a regression test for it.

Two consequences worth remembering:
- After the bot asks a question, that question is the newest message, so the original ❌ is not reprocessed. Idempotency comes from message ordering, not from stored flags.
- An invalid reply is answered with a *new* question carrying `META_FRAGE`, which re-anchors the scan. That is what stops the bot from replying to the same nonsense on every poll.

A member is only removed from their old week once a **valid** target week is confirmed — otherwise a week could silently end up understaffed when someone declines and never answers.

The bot **pre-seeds ✅ and ❌ on its own draw DM** (`PREFILL_REACTIONS`, needs the `reactions:write` scope) so a member only has to click, instead of having to think of reacting and then hit the right emoji — an unrecognised emoji does nothing at all, silently. This only works because `read_dm_history` drops reactions whose only reactor is the bot itself, via `eigene_user_id()` (`auth.test`, cached). Without that filter every draw DM would look like a ❌ and the next poll would ask the whole crew to reschedule. If the bot's own ID cannot be determined, `read_dm_history` deliberately reports *no* reactions at all and says so loudly: a missed reaction is caught by the next poll, a mass false alarm is not.

Both evaluations follow the rule "the newest bot message wins", so each one
filters the history down to **its own message family** first
(`config.RESCHEDULE_EVENTS` / `config.ERLEDIGT_EVENTS`). Without that, an
Erledigt follow-up DM would be the newest message and permanently block a ❌ on
an older draw DM.

Every bot DM carries the member's Notion ID in its metadata payload, and `reschedule.verlauf_fuer` drops bot messages belonging to someone else before the state machine runs. In production each member has their own DM channel, so this filters nothing — but with `SLACK_TEST_USER_ID` *all* DMs land in one channel, and without it a single ❌ would move the whole crew of that week. That is also why the swap confirmation carries `META_BESTAETIGUNG`: an untagged confirmation would anchor every member's scan, not just its recipient's.

## Working notes

- Notion API version `2025-09-03`: `data_source_id` (not `database_id`) as the parent when creating pages, and `/v1/data_sources/{id}/query` for queries. When `template` is used in a page-creation payload, `children` must **not** be present.
- The Putzplan data source needs a **`Jahr` (number)** property. `Kalenderwoche` alone is ambiguous across years, which breaks recency ordering and cross-new-year planning. Pages without `Jahr` are skipped with a warning.
- Weeks with `Status: Nicht auswählen` or `Archiv: true` are never touched.
- The Putzplan database contains **sentinel pages that are not weeks** — "Ausgetragen" (KW 0) and "Postponed" (KW 54), a pre-`Putzstatus` workaround for parking members. `get_week_pages` drops any page whose KW is outside `1..iso_weeks_in_year`, and `putz_count` counts only *resolvable* pages, so these never count as a cleaning shift. Without that guard `week_index` clamps them to KW 53 and everyone parked there looks freshly cleaned. The pages are slated for deletion, but the guard should stay — it also protects against any future non-week page.
- The bot **only ever writes to the Putzplan** data source; the Mitgliederliste is read-only. That is why test setups only need a Putzplan copy. Beware: writing the `Mitglieder` relation in the Putzplan makes Notion write the inverse relation onto member pages in the Mitgliederliste - this is necessary and gets solved in the test setup by a different relation for it: `TEST_PUTZPLAN_RELATION_PROP`. Every other Property of the Mitgliederliste stays untouched.
- User-facing strings, print output, and Notion/Slack property names are German — keep new code consistent rather than mixing in English property names.
- Notion relations return at most 25 items inline; `_relation_ids` logs a debug warning when there are more.
- `venv/` and `.venv/` are both gitignored; either may be present locally.
