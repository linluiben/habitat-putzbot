"""Slack: User-Lookup, Kanal-Nachrichten, DMs und die Texte dazu."""

from slack_sdk.errors import SlackApiError

from config import (
    CHANNEL_HISTORY_LIMIT,
    CONFIRM_REACTIONS,
    DECLINE_REACTIONS,
    DM_HISTORY_LIMIT,
    DM_ONLY,
    DRY_RUN,
    META_AUSLOSUNG,
    META_WOCHE,
    PUTZ_REACTION,
    RESCHEDULE_ENABLED,
    SLACK_CHANNEL_ID,
    SLACK_TEST_USER_ID,
    TRACKING_ENABLED,
    debug,
    slack,
)

_user_id_cache = {}
_email_cache = {}  # Slack-User-ID -> E-Mail (Rueckweg, nur fuer unerwartete Reagierende)
_eigene_id = {}  # Dict statt Variable, damit "noch nicht geholt" von "geht nicht" trennbar bleibt


def eigene_user_id():
    """Die User-ID des Bots selbst. Einmal geholt, dann gecacht.

    Gebraucht, um beim Lesen der Reaktionen die eigenen von denen des Mitglieds
    zu trennen: der Bot setzt ✅/❌ auf seiner Auslos-DM vor, damit man nur noch
    klicken muss. Ohne diese Unterscheidung läse er sein eigenes ❌ als Absage.
    """
    if "id" not in _eigene_id:
        try:
            _eigene_id["id"] = slack.auth_test()["user_id"]
            debug(f"Eigene Bot-User-ID: {_eigene_id['id']}")
        except (SlackApiError, KeyError) as error:
            _eigene_id["id"] = None
            print(f"   ⚠️ Eigene Bot-User-ID nicht ermittelbar: {error}")
    return _eigene_id["id"]


def get_slack_user_id(email):
    """Slack-User-ID zu einer E-Mail. Ergebnis wird gecacht (auch Fehlschläge)."""
    if not email:
        return None
    if email in _user_id_cache:
        return _user_id_cache[email]

    try:
        result = slack.users_lookupByEmail(email=email)
        user_id = result["user"]["id"]
    except SlackApiError as error:
        debug(f"Kein Slack-User für {email}: {error.response['error']}")
        user_id = None

    _user_id_cache[email] = user_id
    return user_id


def vorname(member):
    """'Nachname, Vorname' -> 'Vorname'.

    Fällt auf den vollen Titel zurück, wenn hinter dem Komma nichts steht —
    sonst stünde in der Nachricht eine leere Erwähnung.
    """
    kurz = member["name"].split(",")[-1].strip()
    return kurz or member["name"].strip().rstrip(",").strip() or "?"


def mention(member):
    """@-Erwähnung, sonst der Vorname als Fallback."""
    user_id = get_slack_user_id(member.get("email"))
    return f"<@{user_id}>" if user_id else vorname(member)


def mention_list(members):
    return ", ".join(mention(m) for m in members)


def post_channel(text, channel=None, metadata=None, reaktionen=()):
    """Nachricht in den Kanal. Gibt den Message-Timestamp zurück (sonst None).

    `metadata` haengt strukturiert an der Nachricht und kommt beim Lesen der
    Kanal-Historie wieder mit — so findet der Erledigt-Abgleich die
    Wochennachricht wieder, ohne die KW aus dem Text zurückrechnen zu müssen.

    `reaktionen` setzt der Bot selbst vor, damit man zum Bestätigen nur noch
    klicken muss statt das richtige Emoji suchen zu müssen.
    """
    channel = channel or SLACK_CHANNEL_ID
    if DM_ONLY:
        print(f"   🚫 DM_ONLY — Kanal-Nachricht an {channel} unterdrückt:\n{_indent(text)}")
        return None
    if DRY_RUN:
        print(f"   🧪 [DRY RUN] Kanal-Nachricht an {channel}:\n{_indent(text)}")
        return None

    try:
        kwargs = {"channel": channel, "text": text}
        if metadata:
            kwargs["metadata"] = metadata
        response = slack.chat_postMessage(**kwargs)
        print("   📨 Slack-Nachricht in den Kanal gesendet.")
    except SlackApiError as error:
        print(f"   ❌ Slack-Fehler (Kanal): {error.response['error']}")
        return None

    # Eigener Block, gleiche Begründung wie bei den DMs: scheitert das
    # Vorsetzen, ist die Nachricht trotzdem raus und gültig.
    for emoji in reaktionen:
        try:
            slack.reactions_add(channel=channel, timestamp=response["ts"], name=emoji)
        except SlackApiError as error:
            print(f"   ⚠️ Reaktion :{emoji}: nicht gesetzt: {error.response['error']}")

    return response["ts"]


def dm_channel(member):
    """DM-Kanal-ID für ein Mitglied (öffnet die Konversation, falls nötig)."""
    user_id = SLACK_TEST_USER_ID or get_slack_user_id(member.get("email"))
    if not user_id:
        return None
    try:
        return slack.conversations_open(users=[user_id])["channel"]["id"]
    except SlackApiError as error:
        debug(f"conversations_open für {member['name']} fehlgeschlagen: {error.response['error']}")
        return None


def send_dm(member, text, metadata=None, reaktionen=()):
    """DM an ein Mitglied. Gibt den Message-Timestamp zurück.

    `metadata` wird als Slack-Message-Metadata angehängt und kommt beim Lesen
    der Historie strukturiert zurück — so weiß der Poll-Lauf später, auf welche
    Woche sich eine Reaktion bezieht, ohne den Text parsen zu müssen.

    `reaktionen` setzt der Bot direkt selbst auf die Nachricht, damit das
    Mitglied nur noch klicken muss, statt das passende Emoji suchen zu müssen.
    `read_dm_history` filtert sie beim Lesen wieder heraus.
    """
    if SLACK_TEST_USER_ID:
        text = f"_[Test-DM, eigentlich an {member['name']}]_\n\n{text}"
        user_id = SLACK_TEST_USER_ID
    else:
        user_id = get_slack_user_id(member.get("email"))

    if not user_id:
        print(f"   ⚠️ Keine Slack-ID für {member['name']} — keine DM verschickt.")
        return None

    if DRY_RUN:
        print(f"   🧪 [DRY RUN] DM an {member['name']} ({user_id}):\n{_indent(text)}")
        return None

    try:
        channel = slack.conversations_open(users=[user_id])["channel"]["id"]
        kwargs = {"channel": channel, "text": text}
        if metadata:
            kwargs["metadata"] = metadata
        response = slack.chat_postMessage(**kwargs)
        debug(f"DM an {member['name']} gesendet (ts={response['ts']}).")
    except SlackApiError as error:
        print(f"   ❌ Slack-Fehler (DM an {member['name']}): {error.response['error']}")
        return None

    # Eigener Block: scheitert das Vorsetzen (z.B. fehlender reactions:write-Scope),
    # ist die DM trotzdem raus und gültig. Sie dann wegen der Deko zu verwerfen
    # wäre der schlechtere Tausch — reagieren lässt sich auch ohne Vorlage.
    for emoji in reaktionen:
        try:
            slack.reactions_add(channel=channel, timestamp=response["ts"], name=emoji)
        except SlackApiError as error:
            print(f"   ⚠️ Reaktion :{emoji}: nicht gesetzt: {error.response['error']}")

    return response["ts"]


def read_dm_history(member):
    """Bot-Nachrichten samt Metadata und Reaktionen aus dem DM-Verlauf lesen.

    Gibt eine Liste von Dicts zurück, neueste zuerst:
    `{ts, event_type, payload, reaktionen, ist_vom_bot, text}`.
    """
    channel = dm_channel(member)
    if not channel:
        return []

    try:
        response = slack.conversations_history(
            channel=channel, limit=DM_HISTORY_LIMIT, include_all_metadata=True
        )
    except SlackApiError as error:
        debug(f"conversations_history für {member['name']}: {error.response['error']}")
        return []

    # Der Bot setzt ✅/❌ auf seiner Auslos-DM selbst vor. Ohne diese ID könnte er
    # eigene nicht von fremden Reaktionen trennen und läse sein eigenes ❌ als
    # Absage — und zwar bei JEDEM Mitglied. Dann lieber gar keine Reaktion sehen:
    # eine verpasste holt der nächste Poll nach, einen Massen-Fehlalarm nicht.
    ich = eigene_user_id()
    if ich is None:
        print(
            "   ⚠️ Ohne eigene Bot-User-ID lassen sich vorgesetzte Reaktionen nicht "
            "von echten unterscheiden — dieser Lauf wertet keine Reaktionen aus."
        )

    verlauf = []
    for message in response.get("messages", []):
        metadata = message.get("metadata") or {}
        reaktionen = set() if ich is None else {
            reaction["name"]
            for reaction in message.get("reactions", []) or []
            if set(reaction.get("users") or []) - {ich}
        }
        verlauf.append(
            {
                "ts": message.get("ts"),
                "text": message.get("text", ""),
                # bot_id gesetzt = von uns, sonst vom Mitglied geschrieben
                "ist_vom_bot": bool(message.get("bot_id")),
                "event_type": metadata.get("event_type"),
                "payload": metadata.get("event_payload") or {},
                "reaktionen": reaktionen,
            }
        )

    # Die eine Annahme, auf der der ganze Reschedule-Flow steht: dass Slack die
    # Metadata über conversations_history zurückgibt. Kommt hier 0 heraus,
    # obwohl Bot-Nachrichten dabei sind, fehlt entweder ein Scope oder die
    # Nachrichten stammen von einer Version ohne Metadata.
    vom_bot = sum(1 for e in verlauf if e["ist_vom_bot"])
    mit_meta = sum(1 for e in verlauf if e["event_type"])
    debug(
        f"DM-Verlauf {member['name']}: {len(verlauf)} Nachrichten, "
        f"{vom_bot} vom Bot, {mit_meta} mit Metadata."
    )
    return verlauf


def reaktion_auf(eintrag):
    """'ja' / 'nein' / None — was hat das Mitglied auf diese Nachricht geklickt?"""
    if eintrag["reaktionen"] & DECLINE_REACTIONS:
        return "nein"
    if eintrag["reaktionen"] & CONFIRM_REACTIONS:
        return "ja"
    return None


def slack_id_fuer(member):
    """Slack-ID eines Mitglieds — mit derselben Umleitung wie bei den DMs.

    Mit `SLACK_TEST_USER_ID` bekommen ALLE Mitglieder dieselbe ID. Das ist
    gewollt und die einzige Art, den Erledigt-Abgleich im Sandbox-Workspace zu
    testen: dort existieren die echten E-Mail-Adressen nicht, der Lookup ginge
    für jedes Mitglied ins Leere. Preis: eine Bestätigung der Testperson
    bestätigt die ganze Crew — genauso, wie dort alle DMs bei einer Person
    landen.
    """
    return SLACK_TEST_USER_ID or get_slack_user_id(member.get("email"))


def email_fuer_user(user_id):
    """E-Mail zu einer Slack-ID — der Rückweg zu einem Notion-Mitglied.

    Bewusst einzeln und nur für UNERWARTETE Reagierende: die Crew ist über
    `slack_id_fuer` ohnehin schon aufgelöst, und alle ~60 Mitglieder
    aufzulösen wäre ein Vielfaches an API-Aufrufen für denselben Zweck.
    """
    if user_id in _email_cache:
        return _email_cache[user_id]
    try:
        profil = slack.users_info(user=user_id)["user"].get("profile") or {}
        email = profil.get("email")
    except (SlackApiError, KeyError) as error:
        debug(f"users_info für {user_id} fehlgeschlagen: {error}")
        email = None
    _email_cache[user_id] = email
    return email


def lies_kanal_verlauf(oldest=None, channel=None):
    """Bot-Nachrichten aus dem Kanal, neueste zuerst, samt Metadata.

    Braucht den Scope `channels:history` (bzw. `groups:history`, wenn der Kanal
    privat ist) und setzt voraus, dass der Bot Mitglied des Kanals ist.
    """
    channel = channel or SLACK_CHANNEL_ID
    kwargs = {"channel": channel, "limit": CHANNEL_HISTORY_LIMIT,
              "include_all_metadata": True}
    if oldest:
        kwargs["oldest"] = str(oldest)

    try:
        response = slack.conversations_history(**kwargs)
    except SlackApiError as error:
        fehler = error.response["error"]
        print(f"   ⚠️ Kanal-Historie nicht lesbar: {fehler}")
        if fehler == "missing_scope":
            print("      Fehlt vermutlich `channels:history` (bei privaten Kanälen "
                  "`groups:history`) — App-Scopes prüfen und neu installieren.")
        elif fehler == "not_in_channel":
            print("      Der Bot ist kein Mitglied des Kanals — einladen.")
        return None
    return response.get("messages", [])


def finde_wochennachrichten(oldest=None, channel=None):
    """Wochennachrichten aus dem Kanal als {(kw, jahr): ts}.

    Gibt None zurück, wenn die Historie gar nicht gelesen werden konnte — das
    ist etwas anderes als "keine Nachricht gefunden" und muss vom Aufrufer
    unterschieden werden, sonst würde ein fehlender Scope wie "niemand hat
    geputzt" aussehen.
    """
    messages = lies_kanal_verlauf(oldest=oldest, channel=channel)
    if messages is None:
        return None

    gefunden = {}
    for message in messages:
        metadata = message.get("metadata") or {}
        if metadata.get("event_type") != META_WOCHE:
            continue
        payload = metadata.get("event_payload") or {}
        kw, jahr = payload.get("kw"), payload.get("jahr")
        if kw is None or jahr is None:
            continue
        # Neueste zuerst: eine später wiederholte Nachricht zur selben Woche
        # gewinnt, weil dort die aktuellen Reaktionen stehen.
        gefunden.setdefault((int(kw), int(jahr)), message.get("ts"))

    debug(f"Kanal-Historie: {len(messages)} Nachrichten, {len(gefunden)} Wochennachrichten.")
    return gefunden


def reagierende_user(ts, emoji, channel=None):
    """User-IDs, die mit `emoji` auf eine Nachricht reagiert haben (ohne den Bot).

    Bewusst `reactions.get` statt der Reaktionen aus `conversations_history`:
    dort kann die `users`-Liste gekürzt sein, und eine gekürzte Liste hiesse
    hier "hat nicht geputzt".
    """
    channel = channel or SLACK_CHANNEL_ID
    try:
        antwort = slack.reactions_get(channel=channel, timestamp=ts, full=True)
    except SlackApiError as error:
        print(f"   ⚠️ Reaktionen zu {ts} nicht lesbar: {error.response['error']}")
        return None

    reaktionen = ((antwort.get("message") or {}).get("reactions")) or []
    ich = eigene_user_id()
    for reaktion in reaktionen:
        if reaktion.get("name") == emoji:
            return {uid for uid in (reaktion.get("users") or []) if uid != ich}
    return set()


def wochen_metadata(kw, year):
    return {"event_type": META_WOCHE, "event_payload": {"kw": kw, "jahr": year}}


def erledigt_metadata(event_type, member, kw, year, art=None):
    payload = {"kw": kw, "jahr": year, "mitglied": member["id"]}
    if art:
        payload["art"] = art
    return {"event_type": event_type, "event_payload": payload}


def auslosung_metadata(member, kw, year):
    return {
        "event_type": META_AUSLOSUNG,
        "event_payload": {"kw": kw, "jahr": year, "mitglied": member["id"]},
    }


def _indent(text):
    return "\n".join(f"      | {line}" for line in text.splitlines())


# --- Nachrichtentexte ---

def build_draw_dm(member, kw, page_url):
    """DM an ein frisch ausgelostes Mitglied."""
    text = (
        f"Hallo {vorname(member)}! 🧹\n\n"
        f"Du wurdest für die Putzcrew in *KW {kw}* ausgelost. "
        f"Du bist damit schon für diese Woche eingetragen."
    )
    if RESCHEDULE_ENABLED:
        text += (
            "\n\nPasst dir die Woche?\n"
            "✅ = passt, ich bin dabei\n"
            "❌ = ich möchte in einer anderen Woche putzen\n\n"
            "Reagier einfach mit dem passenden Emoji auf diese Nachricht. "
            "Ich schaue mehrmals am Tag nach, es kann also ein paar Stunden dauern, "
            "bis ich mich melde."
        )
    else:
        text += (
            "\n\nWenn es dir nicht passt, meld dich bitte kurz im Team — "
            "das automatische Verschieben kommt noch."
        )
    if page_url:
        text += f"\n\n👉 <{page_url}|Zur Woche in Notion>"
    return text


def build_wochen_auslosung(kw, bestehend, gelost, page_url):
    """Kanalnachricht für den Übergangsmodus `draw` — eine Woche, keine DMs.

    Bewusst im Ton der alten V2-Nachricht: solange es noch keine DMs gibt, ist
    das hier die einzige Stelle, an der jemand von seinem Einsatz erfährt.
    """
    text = f"🧹 *Der Putzplan für KW {kw} ist da* 🧹\n\n"

    if not bestehend and not gelost:
        text += (
            "Für diese Woche ist noch niemand eingetragen und es konnte auch "
            "niemand ausgelost werden. Wer mag spontan übernehmen?"
        )
    elif not gelost:
        text += (
            f"Diese Woche sind wir schon komplett — danke an die Freiwilligen: "
            f"{mention_list(bestehend)} 💚"
        )
    else:
        if bestehend:
            text += f"Danke fürs freiwillige Eintragen: {mention_list(bestehend)} 🙏\n"
        text += f"Ausgelost wurden: {mention_list(gelost)} 🎲"

    text += build_bestaetigungs_hinweis()
    if page_url:
        text += f"\n\n👉 <{page_url}|Zur Woche in Notion>"
    return text


def build_bestaetigungs_hinweis():
    """Der Absatz, der die :broom:-Bestätigung erklärt.

    Steht unter jeder Wochennachricht, weil die Reaktion sonst niemand als
    Aufforderung liest — und weil ein Emoji ohne Erklärung genauso gut
    "gesehen" heißen kann.

    Hängt am selben Schalter wie die Auswertung: ohne `TRACKING_ENABLED` würde
    hier sonst zum Klicken aufgefordert, während der Bot weder den Besen
    vorsetzt noch je hinschaut — die Aufforderung liefe ins Leere.
    """
    if not TRACKING_ENABLED:
        return ""
    return (
        f"\n\n*Wenn ihr geputzt habt, reagiert bitte auf diese Nachricht mit* :{PUTZ_REACTION}:, "
        f"damit ich erkenne, wer wirklich da war.\n"
        f"_Nur für die, die diese Woche tatsächlich geputzt haben. Wer nicht dazu gekommen ist, klickt einfach nichts "
        f"und wer spontan mitgeholfen hat, darf auch klicken - ich frage dann nochmal per PM nach."
        f"Nutzt gerne auch den Thread, um euch abzusprechen und mitzuteilen, was geputzt wurde._"
    )


def build_erledigt_frage(member, kw, art, frist_text):
    """Nachfrage-PM beim Erledigt-Abgleich.

    `art` ist 'ausgelost' (war eingetragen, hat nicht bestätigt) oder 'zusatz'
    (hat bestätigt, war aber nicht eingetragen).
    """
    if art == "zusatz":
        return (
            f"Hallo {vorname(member)}! 🧹\n\n"
            f"Du hast unter der Nachricht zu *KW {kw}* auf :{PUTZ_REACTION}: geklickt, "
            f"standest dort aber gar nicht im Putzplan. Hast du mitgeputzt?\n"
            f"✅ = ja, trag mich bitte ein\n"
            f"❌ = nein, das war ein Versehen\n\n"
            f"_Wenn ich bis {frist_text} nichts höre, lasse ich KW {kw} so, wie sie ist._"
        )
    return (
        f"Hallo {vorname(member)}! 🧹\n\n"
        f"Du warst für *KW {kw}* in der Putzcrew eingetragen, hast unter der "
        f"Wochennachricht aber nicht auf :{PUTZ_REACTION}: geklickt. Warst du putzen?\n"
        f"✅ = ja, war ich\n"
        f"❌ = nein, hat nicht geklappt\n\n"
        f"_Wenn ich bis {frist_text} nichts höre, trage ich dich aus KW {kw} aus. "
        f"Die Woche zählt dann nicht als dein Putzeinsatz — du kommst also früher "
        f"wieder in den Lostopf._"
    )


def build_erledigt_eingetragen(member, kw, page_url=None):
    text = f"Danke dir! Ich habe dich für *KW {kw}* als Putzeinsatz eingetragen. ✅"
    if page_url:
        text += f"\n\n👉 <{page_url}|Zur Woche in Notion>"
    return text


def build_erledigt_ausgetragen(member, kw, grund):
    return (
        f"Ich habe dich wieder aus *KW {kw}* ausgetragen: {grund}.\n\n"
        f"Die Woche zählt damit nicht als dein Putzeinsatz — du kommst also früher "
        f"wieder in den Lostopf. Falls das nicht stimmt, meld dich einfach kurz bei Team Gemütlichkeit."
    )


def build_reminder(kw, crew, page_url):
    """Wöchentliche Erinnerung in den Kanal."""
    if not crew:
        text = (
            f"🧹 *Putzplan KW {kw}* 🧹\n\n"
            f"Für diese Woche ist noch niemand eingetragen. "
            f"Wer mag spontan übernehmen?"
        )
    else:
        text = (
            f"🧹 *Putzplan KW {kw}* 🧹\n\n"
            f"Diese Woche seid ihr dran: {mention_list(crew)} 💚"
        )
    text += build_bestaetigungs_hinweis()
    if page_url:
        text += f"\n\n👉 <{page_url}|Zur Woche in Notion>"
    return text


def build_reschedule_frage(member, kw, max_kw_hinweis):
    """Nachfrage, nachdem jemand mit ❌ reagiert hat."""
    return (
        f"Alles klar {vorname(member)}, KW {kw} passt dir also nicht. 👍\n\n"
        f"In welcher Woche möchtest du stattdessen putzen?\n"
        f"Antworte einfach mit der Kalenderwoche als Zahl, z.B. `{max_kw_hinweis}`.\n\n"
        f"_Du bleibst so lange in KW {kw} eingetragen, bis du dich für eine neue Woche "
        f"entschieden hast — damit die Woche nicht plötzlich unbesetzt ist._"
    )


def build_reschedule_ok(member, alte_kw, neue_kw, page_url):
    text = (
        f"Erledigt! Du bist jetzt statt in KW {alte_kw} in *KW {neue_kw}* eingetragen. ✅"
    )
    if page_url:
        text += f"\n\n👉 <{page_url}|Zur neuen Woche in Notion>"
    return text


def build_reschedule_fehler(member, eingabe, grund, max_kw_hinweis, link=None):
    """Absage auf eine Wunschwoche, verbunden mit einer erneuten Nachfrage.

    Der Einstieg ist bewusst neutral: dieselbe Funktion bedient unverständliche
    Eingaben *und* verstandene, aber unbrauchbare Wochen (voll, gesperrt, in der
    Vergangenheit). Ein „damit kann ich nichts anfangen" wäre im zweiten Fall
    schlicht falsch — der Bot hat die KW ja verstanden.
    """
    text = (
        f"Das klappt leider nicht mit `{eingabe}`: {grund}\n\n"
        f"Antworte bitte nochmal mit einer Kalenderwoche als Zahl, z.B. "
        f"`{max_kw_hinweis}`."
    )
    if link:
        text += (
            f"\n\n👉 <{link}|Hier siehst du die Wochen in Notion> — "
            f"such dir von dort eine mit weniger als 4 Leuten aus."
        )
    return text


def build_cycle_summary(cycle, year, per_week):
    """Zusammenfassung nach dem Plan-Lauf: wer putzt im nächsten Zyklus wann.

    `per_week` ist eine Liste von (kw, crew, page_url).
    """
    text = f"🗓️ *Putzplan für Zyklus {cycle}/{year} steht* 🗓️\n\n"
    for kw, crew, page_url in per_week:
        names = mention_list(crew) if crew else "_noch offen_"
        line = f"• *KW {kw}*: {names}"
        if page_url:
            line = f"• *<{page_url}|KW {kw}>*: {names}"
        text += line + "\n"
    text += (
        "\nDie Ausgelosten haben eine DM bekommen. "
        "Wer tauschen möchte, meldet sich bitte frühzeitig 🙏"
    )
    return text
