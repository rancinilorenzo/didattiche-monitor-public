from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta

import monitor as core
import monitor_runner as legacy


# ---------------------------------------------------------------------------
# Telegram UI v3
# ---------------------------------------------------------------------------
# Questo file è un wrapper leggero sopra monitor_runner.py:
# lascia intatta la logica di scraping, Calendar, scheduler, retry e Anti-Salto
# e sostituisce soltanto presentazione Telegram + comportamento dei pulsanti.
# ---------------------------------------------------------------------------

SNOOZE_BUTTON_WINDOW_MINUTES = max(
    1,
    int(
        os.getenv(
            "ANTI_SKIP_SNOOZE_BUTTON_WINDOW_MINUTES",
            "120",
        )
    ),
)


def snooze_button_is_useful(
    lesson: core.Lesson,
    now: datetime,
) -> bool:
    """
    Mostra/accetta "Ricordamelo tra 15 min" solo:
    - nelle 2 ore precedenti alla lezione;
    - mentre la didattica è già in corso.
    """
    if now >= lesson.end_dt:
        return False

    minutes = (lesson.start_dt - now).total_seconds() / 60
    return minutes <= SNOOZE_BUTTON_WINDOW_MINUTES


def callback_keyboard(
    lesson: core.Lesson,
    acknowledged: bool = False,
    snoozed: bool = False,
    now: datetime | None = None,
) -> dict:
    """
    Telegram UI v3:
    - "Preso nota" -> "Visto";
    - "Ricordamelo" compare solo quando utile;
    - nessun pulsante "Apri Mercatorum".
    """
    effective_now = now or datetime.now(core.TIMEZONE)

    ack_text = "✅ Visto ✓" if acknowledged else "✅ Visto"

    rows: list[list[dict]] = [
        [
            {
                "text": ack_text,
                "callback_data": legacy.callback_data(
                    "ack",
                    lesson,
                ),
            },
        ],
    ]

    if snooze_button_is_useful(
        lesson,
        effective_now,
    ):
        snooze_text = (
            f"⏰ Tra {legacy.SNOOZE_MINUTES} min ✓"
            if snoozed
            else f"⏰ Ricordamelo tra {legacy.SNOOZE_MINUTES} min"
        )

        rows.append(
            [
                {
                    "text": snooze_text,
                    "callback_data": legacy.callback_data(
                        "snooze",
                        lesson,
                    ),
                },
            ]
        )

    return {
        "inline_keyboard": rows,
    }


def process_telegram_updates(
    lessons: list[core.Lesson],
    meta_by_key: dict[str, dict],
    anti_skip_state: dict,
    now: datetime,
) -> None:
    """
    Mantiene la logica Anti-Salto v2 esistente e modifica solo:
    - testo "Visto";
    - protezione dello snooze oltre le 2 ore;
    - messaggi di conferma più espliciti.
    """
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return

    main_chat = legacy.telegram_main_chat_id()
    if not main_chat:
        return

    lesson_by_key = {
        lesson.exact_key: lesson
        for lesson in lessons
    }

    acknowledged = anti_skip_state.setdefault(
        "acknowledged",
        {},
    )
    snoozes = anti_skip_state.setdefault(
        "snoozes",
        {},
    )

    offset = int(
        anti_skip_state.get(
            "telegram_update_offset",
            0,
        )
        or 0
    )

    for _ in range(5):
        try:
            response = legacy.telegram_api(
                "getUpdates",
                {
                    "offset": offset,
                    "limit": 100,
                    "timeout": 0,
                    "allowed_updates": [
                        "callback_query",
                        "message",
                    ],
                },
            )
        except Exception as exc:
            print(
                "Lettura interazioni Telegram non riuscita: "
                f"{type(exc).__name__}"
            )
            return

        updates = response.get("result", [])
        if not updates:
            break

        for update in updates:
            update_id = int(
                update.get(
                    "update_id",
                    0,
                )
            )

            offset = max(
                offset,
                update_id + 1,
            )

            anti_skip_state[
                "telegram_update_offset"
            ] = offset

            message_update = update.get("message")
            if isinstance(
                message_update,
                dict,
            ):
                legacy.process_private_telegram_command(
                    message_update,
                    anti_skip_state,
                )
                continue

            query = update.get("callback_query")
            if not isinstance(query, dict):
                continue

            query_id = str(
                query.get(
                    "id",
                    "",
                )
            )
            data = str(
                query.get(
                    "data",
                    "",
                )
            )

            message = query.get("message") or {}
            chat = message.get("chat") or {}
            from_user = query.get("from") or {}

            chat_id = str(
                chat.get(
                    "id",
                    "",
                )
            )
            user_id = str(
                from_user.get(
                    "id",
                    "",
                )
            )

            allowed_chats = (
                legacy.telegram_allowed_callback_chats(
                    anti_skip_state,
                )
            )

            if chat_id not in allowed_chats:
                legacy.answer_callback(
                    query_id,
                    (
                        "Questo pulsante non è "
                        "autorizzato in questa chat."
                    ),
                    alert=True,
                )
                continue

            owner_id = legacy.telegram_owner_user_id(
                anti_skip_state,
            )

            if owner_id and user_id != owner_id:
                legacy.answer_callback(
                    query_id,
                    (
                        "Questo pulsante è riservato "
                        "al proprietario del monitor."
                    ),
                    alert=True,
                )
                continue

            match = re.fullmatch(
                (
                    rf"{re.escape(legacy.CALLBACK_PREFIX)}:"
                    r"(ack|snooze):([0-9a-f]{16})"
                ),
                data,
            )

            if not match:
                legacy.answer_callback(
                    query_id,
                    "Pulsante non riconosciuto.",
                )
                continue

            action, lesson_key = match.groups()

            lesson = lesson_by_key.get(
                lesson_key
            )

            if lesson is None:
                legacy.answer_callback(
                    query_id,
                    (
                        "Questa programmazione "
                        "non è più attuale."
                    ),
                    alert=True,
                )
                continue

            if now >= lesson.end_dt:
                legacy.answer_callback(
                    query_id,
                    (
                        "Questa didattica risulta "
                        "già terminata."
                    ),
                    alert=True,
                )
                continue

            user_name = core.normalize_space(
                " ".join(
                    value
                    for value in [
                        str(
                            from_user.get(
                                "first_name",
                                "",
                            )
                        ),
                        str(
                            from_user.get(
                                "last_name",
                                "",
                            )
                        ),
                    ]
                    if value
                )
            )

            if action == "ack":
                acknowledged[lesson_key] = {
                    "at": now.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                }

                snoozes.pop(
                    lesson_key,
                    None,
                )

                legacy.answer_callback(
                    query_id,
                    (
                        "✅ Visto. I promemoria "
                        "importanti restano attivi."
                    ),
                )

                legacy.refresh_callback_keyboard(
                    query,
                    lesson,
                    acknowledged=True,
                    snoozed=False,
                )
                continue

            if action == "snooze":
                # Protegge anche i vecchi messaggi Telegram che
                # contengono ancora il pulsante Ricordamelo.
                if not snooze_button_is_useful(
                    lesson,
                    now,
                ):
                    legacy.answer_callback(
                        query_id,
                        (
                            "⏰ Disponibile solo nelle 2 ore "
                            "prima della lezione o mentre "
                            "è in corso."
                        ),
                        alert=True,
                    )

                    legacy.refresh_callback_keyboard(
                        query,
                        lesson,
                        acknowledged=(
                            legacy.acknowledgement_is_recent(
                                lesson,
                                anti_skip_state,
                                now,
                            )
                        ),
                        snoozed=False,
                    )
                    continue

                due_at = now + timedelta(
                    minutes=legacy.SNOOZE_MINUTES
                )

                acknowledged[lesson_key] = {
                    "at": now.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                }

                snoozes[lesson_key] = {
                    "due_at": due_at.isoformat(),
                    "created_at": now.isoformat(),
                    "lesson": asdict(lesson),
                    "meta": meta_by_key.get(
                        lesson_key,
                        legacy.fallback_meta(
                            lesson
                        ),
                    ),
                    "user_id": user_id,
                    "user_name": user_name,
                }

                legacy.answer_callback(
                    query_id,
                    (
                        f"⏰ Ti ricordo questa didattica tra "
                        f"{legacy.SNOOZE_MINUTES} minuti. "
                        "I promemoria importanti restano attivi."
                    ),
                )

                legacy.refresh_callback_keyboard(
                    query,
                    lesson,
                    acknowledged=True,
                    snoozed=True,
                )

        if len(updates) < 100:
            break


# ---------------------------------------------------------------------------
# Formato uniforme dei messaggi
# ---------------------------------------------------------------------------

def lesson_identity_lines(
    lesson: core.Lesson,
    meta: dict | None = None,
) -> list[str]:
    meta = meta or {}

    subject = core.normalize_space(
        meta.get(
            "subject",
            "",
        )
    )
    title = core.normalize_space(
        meta.get(
            "title",
            "",
        )
    )

    lines: list[str] = []

    if subject:
        lines.append(
            f"🎓 **Materia:** **{subject}**"
        )

    if title:
        lines.append(
            f"📘 **Lezione:** **{title}**"
        )

    if not subject and not title:
        lines.append(
            (
                "📘 **Lezione:** "
                f"**{legacy.lesson_name(lesson)}**"
            )
        )

    return lines


def lesson_schedule_lines(
    lesson: core.Lesson,
) -> list[str]:
    return [
        (
            "🗓️ **Data:** "
            f"{legacy.fmt_date(lesson.date)}"
        ),
        (
            "🕒 **Orario:** "
            f"{lesson.start}–{lesson.end}"
        ),
    ]


def lesson_card(
    lesson: core.Lesson,
    meta: dict | None = None,
) -> str:
    lines = lesson_identity_lines(
        lesson,
        meta,
    )

    lines += [
        "",
        *lesson_schedule_lines(
            lesson
        ),
    ]

    return "\n".join(lines)


def modified_card(
    before: core.Lesson,
    after: core.Lesson,
    before_meta: dict | None,
    after_meta: dict | None,
) -> str:
    before_meta = (
        before_meta
        or legacy.fallback_meta(before)
    )
    after_meta = (
        after_meta
        or legacy.fallback_meta(after)
    )

    date_changed = (
        before.date != after.date
    )

    time_changed = (
        (before.start, before.end)
        !=
        (after.start, after.end)
    )

    if date_changed and time_changed:
        header = (
            "🗓️🕒 GIORNO E ORARIO MODIFICATI"
        )
    elif date_changed:
        header = "🗓️ GIORNO MODIFICATO"
    elif time_changed:
        header = "🕒 ORARIO MODIFICATO"
    else:
        header = "✏️ DIDATTICA MODIFICATA"

    old_subject = core.normalize_space(
        before_meta.get(
            "subject",
            "",
        )
    )
    old_title = core.normalize_space(
        before_meta.get(
            "title",
            "",
        )
    )

    new_subject = core.normalize_space(
        after_meta.get(
            "subject",
            "",
        )
    )
    new_title = core.normalize_space(
        after_meta.get(
            "title",
            "",
        )
    )

    identity_changed = (
        legacy.normalize_identity(old_subject)
        != legacy.normalize_identity(new_subject)
        or
        legacy.normalize_identity(old_title)
        != legacy.normalize_identity(new_title)
    )

    lines = [
        header,
        "",
        *lesson_identity_lines(
            after,
            after_meta,
        ),
    ]

    if date_changed or time_changed:
        lines += [
            "",
            "**PRIMA**",
            *lesson_schedule_lines(
                before
            ),
            "",
            "**ORA**",
            *lesson_schedule_lines(
                after
            ),
        ]

    # Mostra il confronto del testo soltanto se materia/titolo
    # sono davvero cambiati, non per una pura modifica d'orario.
    if identity_changed:
        lines += [
            "",
            "**DIDATTICA PRIMA**",
            *lesson_identity_lines(
                before,
                before_meta,
            ),
            "",
            "**DIDATTICA ORA**",
            *lesson_identity_lines(
                after,
                after_meta,
            ),
        ]

    return "\n".join(lines)


def recovery_card(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
) -> str:
    old_subject = core.normalize_space(
        old_meta.get(
            "subject",
            "",
        )
    )
    old_title = core.normalize_space(
        old_meta.get(
            "title",
            "",
        )
    )

    new_subject = core.normalize_space(
        new_meta.get(
            "subject",
            "",
        )
    )
    new_title = core.normalize_space(
        new_meta.get(
            "title",
            "",
        )
    )

    same_schedule = (
        old_lesson.date == new_lesson.date
        and old_lesson.start == new_lesson.start
        and old_lesson.end == new_lesson.end
    )

    same_identity = (
        legacy.normalize_identity(old_subject)
        == legacy.normalize_identity(new_subject)
        and
        legacy.normalize_identity(old_title)
        == legacy.normalize_identity(new_title)
    )

    restored = (
        same_schedule
        and same_identity
    )

    if restored:
        header = "♻️ DIDATTICA RIPRISTINATA"
    elif certain:
        header = "♻️ DIDATTICA RECUPERATA"
    else:
        header = (
            "♻️ POSSIBILE RECUPERO / "
            "RIPROGRAMMAZIONE"
        )

    display_meta = {
        "subject": (
            new_subject
            or old_subject
        ),
        "title": (
            new_title
            or old_title
        ),
    }

    lines = [
        header,
        "",
        *lesson_identity_lines(
            new_lesson,
            display_meta,
        ),
    ]

    if restored:
        lines += [
            "",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]
        return "\n".join(lines)

    if same_schedule:
        # L'orario è identico: non ripetiamo due blocchi
        # di programmazione uguali.
        lines += [
            "",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]
    else:
        lines += [
            "",
            "**VECCHIA PROGRAMMAZIONE**",
            *lesson_schedule_lines(
                old_lesson
            ),
            "",
            "**NUOVA PROGRAMMAZIONE**",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]

    if not same_identity:
        lines += [
            "",
            "**DIDATTICA PRIMA**",
            *lesson_identity_lines(
                old_lesson,
                old_meta,
            ),
            "",
            "**DIDATTICA ORA**",
            *lesson_identity_lines(
                new_lesson,
                new_meta,
            ),
        ]

    return "\n".join(lines)


def notify_recovery_anti_skip(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
    now: datetime,
    reminded: set[str],
    anti_skip_state: dict,
) -> None:
    """
    Conserva il significato del messaggio di recupero:
    "RIPRISTINATA" non viene trasformato nel generico
    "RECUPERO / RIPROGRAMMAZIONE".
    """
    message = recovery_card(
        old_lesson,
        new_lesson,
        old_meta,
        new_meta,
        certain,
    )

    title, _, body = message.partition("\n")
    body = body.lstrip()

    base_title = (
        title.removeprefix("♻️ ")
        .strip()
    )

    minutes = (
        new_lesson.start_dt - now
    ).total_seconds() / 60

    if (
        new_lesson.start_dt
        <= now
        < new_lesson.end_dt
    ):
        title = (
            f"🚨 {base_title} · GIÀ IN CORSO"
        )

        body += (
            "\n\n⚠️ Controlla subito Mercatorum."
        )

        legacy.mark_current_notice_as_covered(
            new_lesson,
            now,
            reminded,
        )

    elif minutes <= 180:
        title = f"🚨 {base_title}"

        body += (
            "\n\n⚠️ Mancano circa "
            f"**{legacy.human_minutes(minutes)}**."
        )

        legacy.mark_current_notice_as_covered(
            new_lesson,
            now,
            reminded,
        )

    elif minutes <= 1440:
        title = f"⚠️ {base_title}"

        body += (
            "\n\nMancano circa "
            f"**{legacy.human_minutes(minutes)}**."
        )

        legacy.mark_current_notice_as_covered(
            new_lesson,
            now,
            reminded,
        )

    legacy.notify_lesson(
        title,
        body,
        new_lesson,
        anti_skip_state,
        now,
        urgent=title.startswith("🚨"),
    )



# ---------------------------------------------------------------------------
# Telegram UI v3.1 - riconoscimento ufficiale Materia / Lezione
# ---------------------------------------------------------------------------

OFFICIAL_MERCATORUM_SUBJECTS = (
    # Primo anno
    "Statistical Learning e Analisi dei Big Data",
    "Sicurezza e protezione dei dati e dei sistemi informatici",
    "Economia e gestione dell'innovazione",
    "Tecnologie e sicurezza delle reti di comunicazione",
    "Elementi di diritto penale e criminalità informatica",
    "Gestione del rischio e continuità operativa",
    "Altre conoscenze utili per l'inserimento nel mondo del lavoro",
    "OFA - Area linguistica",
    "Lingua inglese",

    # Secondo anno
    "Cybersecurity",
    "Informatica giuridica ed etica digitale",
    "Principi e Metodi di Crittografia",
    "Informatica Forense e Sicurezza dell'IA",
    "Metodi Statistici per l'Economia Digitale",
    "Diritto dei dati e delle informazioni",
)


def _subject_match_text(
    value: str,
) -> str:
    """
    Normalizzazione usata esclusivamente per confrontare i nomi
    degli insegnamenti. Non modifica il testo mostrato su Telegram.
    """
    return (
        core.normalize_space(value)
        .casefold()
        .replace("’", "'")
        .replace("`", "'")
    )


def split_official_subject(
    value: str,
) -> tuple[str, str] | None:
    """
    Cerca un insegnamento ufficiale all'inizio di una stringa del tipo:

        Sicurezza e protezione ... L'architettura della Resilienza

    e restituisce:

        (
            "Sicurezza e protezione ...",
            "L'architettura della Resilienza",
        )

    Si prova prima il nome più lungo per evitare match prematuri.
    """
    clean = core.normalize_space(value)

    if not clean:
        return None

    ordered_subjects = sorted(
        OFFICIAL_MERCATORUM_SUBJECTS,
        key=len,
        reverse=True,
    )

    clean_match = _subject_match_text(
        clean
    )

    for subject in ordered_subjects:
        official = core.normalize_space(
            subject
        )

        official_match = _subject_match_text(
            official
        )

        if clean_match == official_match:
            return official, ""

        if not clean_match.startswith(
            official_match
        ):
            continue

        prefix = clean[:len(official)]

        if (
            _subject_match_text(prefix)
            != official_match
        ):
            continue

        if (
            len(clean) > len(official)
            and not clean[len(official)].isspace()
        ):
            continue

        remainder = core.normalize_space(
            clean[len(official):]
        )

        return official, remainder

    return None


def normalize_lesson_meta(
    lesson: core.Lesson,
    meta: dict | None = None,
) -> dict:
    """
    Restituisce sempre la miglior coppia Materia / Lezione disponibile.

    Priorità:
    1. subject/title già separati dal parser DOM;
    2. riconoscimento da elenco ufficiale;
    3. vecchio fallback, senza inventare separazioni.
    """
    meta = dict(meta or {})

    subject = core.normalize_space(
        meta.get(
            "subject",
            "",
        )
    )

    title = core.normalize_space(
        meta.get(
            "title",
            "",
        )
    )

    mercatorum_id = core.normalize_space(
        meta.get(
            "mercatorum_id",
            "",
        )
    )

    # Se il parser ha già trovato una materia, preserviamola.
    # Uniformiamo però la grafia con quella ufficiale, quando possibile.
    if subject:
        subject_match = _subject_match_text(
            subject
        )

        for official in OFFICIAL_MERCATORUM_SUBJECTS:
            if (
                _subject_match_text(official)
                == subject_match
            ):
                subject = official
                break

        # Protezione contro eventuale titolo che contenga
        # nuovamente la materia come prefisso.
        if title:
            title_split = split_official_subject(
                title
            )

            if (
                title_split is not None
                and _subject_match_text(
                    title_split[0]
                )
                == _subject_match_text(
                    subject
                )
                and title_split[1]
            ):
                title = title_split[1]

        return {
            "subject": subject,
            "title": title,
            "mercatorum_id": mercatorum_id,
        }

    # Il caso problematico osservato: subject vuoto e title contenente
    # "Materia Titolo della lezione".
    candidates = []

    if title:
        candidates.append(
            title
        )

    description = core.normalize_space(
        lesson.description
    )

    if (
        description
        and description not in candidates
    ):
        candidates.append(
            description
        )

    for candidate in candidates:
        split = split_official_subject(
            candidate
        )

        if split is None:
            continue

        detected_subject, detected_title = split

        if detected_title:
            return {
                "subject": detected_subject,
                "title": detected_title,
                "mercatorum_id": mercatorum_id,
            }

    # Nessun insegnamento ufficiale riconosciuto:
    # manteniamo il vecchio comportamento prudente.
    return {
        "subject": subject,
        "title": (
            title
            or description
            or legacy.lesson_name(lesson)
        ),
        "mercatorum_id": mercatorum_id,
    }


_legacy_fallback_meta_v3 = legacy.fallback_meta


def fallback_meta(
    lesson: core.Lesson,
) -> dict:
    """
    Fallback V3.1 usato anche durante lo scraping.
    Se la descrizione comincia con una delle materie ufficiali,
    ricostruisce subject e title prima di salvare lesson_meta.
    """
    original = _legacy_fallback_meta_v3(
        lesson
    )

    return normalize_lesson_meta(
        lesson,
        original,
    )


def lesson_identity_lines(
    lesson: core.Lesson,
    meta: dict | None = None,
) -> list[str]:
    clean_meta = normalize_lesson_meta(
        lesson,
        meta,
    )

    subject = core.normalize_space(
        clean_meta.get(
            "subject",
            "",
        )
    )

    title = core.normalize_space(
        clean_meta.get(
            "title",
            "",
        )
    )

    lines: list[str] = []

    if subject:
        lines.append(
            f"🎓 **Materia:** **{subject}**"
        )

    if title:
        lines.append(
            f"📘 **Lezione:** **{title}**"
        )

    if not subject and not title:
        lines.append(
            (
                "📘 **Lezione:** "
                f"**{legacy.lesson_name(lesson)}**"
            )
        )

    return lines


def modified_card(
    before: core.Lesson,
    after: core.Lesson,
    before_meta: dict | None,
    after_meta: dict | None,
) -> str:
    before_meta = normalize_lesson_meta(
        before,
        (
            before_meta
            or fallback_meta(before)
        ),
    )

    after_meta = normalize_lesson_meta(
        after,
        (
            after_meta
            or fallback_meta(after)
        ),
    )

    date_changed = (
        before.date != after.date
    )

    time_changed = (
        (before.start, before.end)
        !=
        (after.start, after.end)
    )

    if date_changed and time_changed:
        header = (
            "🗓️🕒 GIORNO E ORARIO MODIFICATI"
        )

    elif date_changed:
        header = "🗓️ GIORNO MODIFICATO"

    elif time_changed:
        header = "🕒 ORARIO MODIFICATO"

    else:
        header = "✏️ DIDATTICA MODIFICATA"

    old_subject = core.normalize_space(
        before_meta.get(
            "subject",
            "",
        )
    )

    old_title = core.normalize_space(
        before_meta.get(
            "title",
            "",
        )
    )

    new_subject = core.normalize_space(
        after_meta.get(
            "subject",
            "",
        )
    )

    new_title = core.normalize_space(
        after_meta.get(
            "title",
            "",
        )
    )

    identity_changed = (
        legacy.normalize_identity(
            old_subject
        )
        != legacy.normalize_identity(
            new_subject
        )
        or
        legacy.normalize_identity(
            old_title
        )
        != legacy.normalize_identity(
            new_title
        )
    )

    lines = [
        header,
        "",
        *lesson_identity_lines(
            after,
            after_meta,
        ),
    ]

    if date_changed or time_changed:
        lines += [
            "",
            "**PRIMA**",
            *lesson_schedule_lines(
                before
            ),
            "",
            "**ORA**",
            *lesson_schedule_lines(
                after
            ),
        ]

    if identity_changed:
        lines += [
            "",
            "**DIDATTICA PRIMA**",
            *lesson_identity_lines(
                before,
                before_meta,
            ),
            "",
            "**DIDATTICA ORA**",
            *lesson_identity_lines(
                after,
                after_meta,
            ),
        ]

    return "\n".join(lines)


def recovery_card(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
) -> str:
    old_meta = normalize_lesson_meta(
        old_lesson,
        old_meta,
    )

    new_meta = normalize_lesson_meta(
        new_lesson,
        new_meta,
    )

    old_subject = core.normalize_space(
        old_meta.get(
            "subject",
            "",
        )
    )

    old_title = core.normalize_space(
        old_meta.get(
            "title",
            "",
        )
    )

    new_subject = core.normalize_space(
        new_meta.get(
            "subject",
            "",
        )
    )

    new_title = core.normalize_space(
        new_meta.get(
            "title",
            "",
        )
    )

    same_schedule = (
        old_lesson.date
        == new_lesson.date
        and old_lesson.start
        == new_lesson.start
        and old_lesson.end
        == new_lesson.end
    )

    same_identity = (
        legacy.normalize_identity(
            old_subject
        )
        == legacy.normalize_identity(
            new_subject
        )
        and
        legacy.normalize_identity(
            old_title
        )
        == legacy.normalize_identity(
            new_title
        )
    )

    restored = (
        same_schedule
        and same_identity
    )

    if restored:
        header = (
            "♻️ DIDATTICA RIPRISTINATA"
        )

    elif certain:
        header = (
            "♻️ DIDATTICA RECUPERATA"
        )

    else:
        header = (
            "♻️ POSSIBILE RECUPERO / "
            "RIPROGRAMMAZIONE"
        )

    display_meta = {
        "subject": (
            new_subject
            or old_subject
        ),
        "title": (
            new_title
            or old_title
        ),
    }

    lines = [
        header,
        "",
        *lesson_identity_lines(
            new_lesson,
            display_meta,
        ),
    ]

    if restored:
        lines += [
            "",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]

        return "\n".join(lines)

    if same_schedule:
        lines += [
            "",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]

    if not same_schedule:
        lines += [
            "",
            "**VECCHIA PROGRAMMAZIONE**",
            *lesson_schedule_lines(
                old_lesson
            ),
            "",
            "**NUOVA PROGRAMMAZIONE**",
            *lesson_schedule_lines(
                new_lesson
            ),
        ]

    if not same_identity:
        lines += [
            "",
            "**DIDATTICA PRIMA**",
            *lesson_identity_lines(
                old_lesson,
                old_meta,
            ),
            "",
            "**DIDATTICA ORA**",
            *lesson_identity_lines(
                new_lesson,
                new_meta,
            ),
        ]

    return "\n".join(lines)




# ---------------------------------------------------------------------------
# Telegram UI v3.2
# Punti bonus 1/1 -> materia completata
# ---------------------------------------------------------------------------

PROGRESS_BONUS_STATE_KEY = "progress_bonus_v1"

PROGRESS_CHECK_INTERVAL_MINUTES = max(
    5,
    int(
        os.getenv(
            "PROGRESS_CHECK_INTERVAL_MINUTES",
            "15",
        )
    ),
)

PROGRESS_BONUS_CONFIRM_CHECKS = max(
    2,
    int(
        os.getenv(
            "PROGRESS_BONUS_CONFIRM_CHECKS",
            "2",
        )
    ),
)

_ACTIVE_COMPLETED_SUBJECTS: set[str] = set()

_core_load_state_v32 = core.load_state
_legacy_scrape_snapshot_v32 = legacy.scrape_snapshot


def canonical_subject(
    value: str,
) -> str:
    value_match = _subject_match_text(
        value
    )

    for official in OFFICIAL_MERCATORUM_SUBJECTS:
        if (
            _subject_match_text(official)
            == value_match
        ):
            return official

    return ""


def completed_subject_keys(
    subjects,
) -> set[str]:
    return {
        _subject_match_text(subject)
        for subject in subjects
        if canonical_subject(subject)
    }


def parse_progress_rows(
    rows: list[str],
) -> dict[str, dict]:
    """
    Converte le righe della pagina I miei progressi in:

        {
            "Materia": {
                "earned": 1,
                "total": 1,
            }
        }

    Accetta soltanto nomi presenti nell'elenco ufficiale.
    """
    result: dict[str, dict] = {}

    for raw in rows:
        clean = core.normalize_space(
            raw
        )

        if not clean:
            continue

        clean_match = _subject_match_text(
            clean
        )

        matches = [
            subject
            for subject in OFFICIAL_MERCATORUM_SUBJECTS
            if _subject_match_text(subject) in clean_match
        ]

        if not matches:
            continue

        subject = max(
            matches,
            key=len,
        )

        bonus = re.search(
            r"punti\s+bonus\s+(\d+)\s+su\s+(\d+)",
            clean,
            flags=re.IGNORECASE,
        )

        if bonus is None:
            continue

        earned = int(
            bonus.group(1)
        )

        total = int(
            bonus.group(2)
        )

        result[subject] = {
            "earned": earned,
            "total": total,
        }

    return result


def progress_rows_from_page(
    page,
) -> list[str]:
    """
    Prende il contenitore più piccolo che contiene:
    - Insegnamento
    - Punti bonus
    - n su n

    Non usa OCR e non dipende dalle coordinate visive.
    """
    return page.evaluate(
        r"""
        () => {
          const normalize = value =>
            (value || '')
              .replace(/\s+/g, ' ')
              .trim()
              .toLowerCase();

          const leaves = Array.from(
            document.querySelectorAll('body *')
          ).filter(el =>
            el.children.length === 0 &&
            normalize(el.textContent) === 'punti bonus'
          );

          const output = [];
          const seen = new Set();

          for (const leaf of leaves) {
            let node = leaf;
            let row = null;

            for (
              let depth = 0;
              depth < 12 && node.parentElement;
              depth++
            ) {
              node = node.parentElement;

              const text = (
                node.innerText || ''
              ).trim();

              if (
                /insegnamento/i.test(text) &&
                /punti\s+bonus/i.test(text) &&
                /\d+\s+su\s+\d+/i.test(text) &&
                text.length >= 20 &&
                text.length <= 2500
              ) {
                row = node;
                break;
              }
            }

            if (!row) {
              continue;
            }

            const text = (
              row.innerText || ''
            ).trim();

            const key = text.replace(
              /\s+/g,
              ' '
            );

            if (seen.has(key)) {
              continue;
            }

            seen.add(key);
            output.push(text);
          }

          return output;
        }
        """
    )


def scrape_progress_bonus_once() -> dict[str, dict]:
    """
    Controllo separato e prudente di I miei progressi.

    Un errore qui NON invalida lo snapshot Programmate:
    il monitor continua normalmente usando gli eventuali
    completamenti già confermati nello stato cifrato.
    """
    with legacy.sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
        )

        context = browser.new_context(
            locale="it-IT",
            timezone_id=core.TIMEZONE_NAME,
        )

        page = context.new_page()

        try:
            page.goto(
                core.SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )

            core.settle_spa(
                page,
                2000,
            )

            core.login_if_needed(
                page
            )

            page.goto(
                core.SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )

            core.settle_spa(
                page,
                2000,
            )

            if core.first_visible(
                page,
                [
                    "#password",
                    "input[type='password']",
                ],
            ):
                core.login_if_needed(
                    page
                )

                page.goto(
                    core.SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )

                core.settle_spa(
                    page,
                    2000,
                )

            if core.first_visible(
                page,
                [
                    "#password",
                    "input[type='password']",
                ],
            ):
                raise RuntimeError(
                    "Mercatorum mostra ancora la pagina di login."
                )

            tab = core.first_visible(
                page,
                [
                    "[role='tab']:has-text('I miei progressi')",
                    "button:has-text('I miei progressi')",
                    "a:has-text('I miei progressi')",
                    "text=I miei progressi",
                ],
            )

            if tab is None:
                raise RuntimeError(
                    "Scheda I miei progressi non trovata."
                )

            tab.click(
                timeout=10_000
            )

            core.settle_spa(
                page,
                1800,
            )

            rows = progress_rows_from_page(
                page
            )

            progress = parse_progress_rows(
                rows
            )

            # Primo anno = 9 righe, secondo anno = 6.
            # Se ne riconosciamo meno di 3 consideriamo la pagina
            # incompleta e NON cambiamo alcuno stato.
            if len(progress) < 3:
                raise RuntimeError(
                    "Pagina progressi incompleta o non riconosciuta."
                )

            core.best_effort_logout(
                page
            )

            return progress

        finally:
            context.close()
            browser.close()


def scrape_progress_bonus_with_retry() -> dict[str, dict] | None:
    for attempt in range(1, 3):
        try:
            return scrape_progress_bonus_once()

        except Exception as exc:
            print(
                "Controllo Punti bonus non riuscito "
                f"({attempt}/2): {type(exc).__name__}"
            )

            if attempt < 2:
                legacy.time.sleep(
                    5
                )

    return None


def progress_control_from_state(
    state: dict,
) -> dict:
    anti_skip = state.get(
        "anti_skip",
        {},
    )

    if not isinstance(
        anti_skip,
        dict,
    ):
        return {}

    control = anti_skip.get(
        PROGRESS_BONUS_STATE_KEY,
        {},
    )

    return (
        dict(control)
        if isinstance(control, dict)
        else {}
    )


def progress_check_is_due(
    control: dict,
    now: datetime,
) -> bool:
    completed = set(
        control.get(
            "completed_subjects",
            [],
        )
    )

    candidates = control.get(
        "candidates",
        {},
    )

    if isinstance(
        candidates,
        dict,
    ):
        for subject, entry in candidates.items():
            if subject in completed:
                continue

            if isinstance(entry, dict):
                if int(entry.get("count", 0) or 0) > 0:
                    return True

    last_success_raw = str(
        control.get(
            "last_success_at",
            "",
        )
        or ""
    )

    if not last_success_raw:
        return True

    try:
        last_success = datetime.fromisoformat(
            last_success_raw
        )

        if last_success.tzinfo is None:
            last_success = last_success.replace(
                tzinfo=core.TIMEZONE
            )

    except Exception:
        return True

    return (
        now - last_success
        >= timedelta(
            minutes=PROGRESS_CHECK_INTERVAL_MINUTES
        )
    )


def update_progress_control(
    state: dict,
    progress: dict[str, dict],
    now: datetime,
) -> tuple[dict, set[str], set[str]]:
    """
    Una materia passa a completata soltanto dopo
    PROGRESS_BONUS_CONFIRM_CHECKS letture positive separate.

    Una volta completata rimane sticky:
    un eventuale glitch 0/1 non la riattiva.
    """
    anti_skip = dict(
        state.get(
            "anti_skip",
            {},
        )
    )

    control = progress_control_from_state(
        state
    )

    completed = {
        canonical_subject(subject)
        for subject in control.get(
            "completed_subjects",
            [],
        )
        if canonical_subject(subject)
    }

    candidates_raw = control.get(
        "candidates",
        {},
    )

    candidates = (
        dict(candidates_raw)
        if isinstance(candidates_raw, dict)
        else {}
    )

    newly_completed: set[str] = set()

    for subject, values in progress.items():
        official = canonical_subject(
            subject
        )

        if not official:
            continue

        if official in completed:
            candidates.pop(
                official,
                None,
            )
            continue

        earned = int(
            values.get(
                "earned",
                0,
            )
            or 0
        )

        total = int(
            values.get(
                "total",
                0,
            )
            or 0
        )

        if not (
            earned == 1
            and total == 1
        ):
            candidates.pop(
                official,
                None,
            )
            continue

        previous = candidates.get(
            official,
            {},
        )

        if not isinstance(
            previous,
            dict,
        ):
            previous = {}

        count = int(
            previous.get(
                "count",
                0,
            )
            or 0
        ) + 1

        entry = {
            "count": count,
            "last_seen": now.isoformat(),
        }

        if previous.get(
            "first_seen"
        ):
            entry["first_seen"] = previous[
                "first_seen"
            ]

        if not previous.get(
            "first_seen"
        ):
            entry["first_seen"] = now.isoformat()

        if count >= PROGRESS_BONUS_CONFIRM_CHECKS:
            completed.add(
                official
            )

            newly_completed.add(
                official
            )

            candidates.pop(
                official,
                None,
            )

            continue

        candidates[official] = entry

    control[
        "completed_subjects"
    ] = sorted(
        completed,
        key=str.casefold,
    )

    control[
        "candidates"
    ] = candidates

    control[
        "last_success_at"
    ] = now.isoformat()

    anti_skip[
        PROGRESS_BONUS_STATE_KEY
    ] = control

    state["anti_skip"] = anti_skip

    return (
        state,
        completed,
        newly_completed,
    )


def lesson_subject(
    lesson: core.Lesson,
    meta: dict | None,
) -> str:
    clean_meta = normalize_lesson_meta(
        lesson,
        meta,
    )

    return canonical_subject(
        clean_meta.get(
            "subject",
            "",
        )
    )


def lesson_is_completed(
    lesson: core.Lesson,
    meta: dict | None,
    completed: set[str],
) -> bool:
    subject = lesson_subject(
        lesson,
        meta,
    )

    if not subject:
        return False

    return (
        _subject_match_text(subject)
        in completed_subject_keys(
            completed
        )
    )


def filter_state_for_completed(
    state: dict,
    completed: set[str],
) -> dict:
    """
    Elimina dallo stato funzionale le lezioni di materie
    già completate, compresi callback/snooze/pending.

    Non modifica direttamente Google Calendar:
    la pulizia Calendar viene fatta separatamente e solo
    sugli eventi managedBy=mercatorum-monitor.
    """
    copied = legacy.json.loads(
        legacy.json.dumps(
            state,
            ensure_ascii=False,
        )
    )

    if not completed:
        return copied

    old_meta = dict(
        copied.get(
            "lesson_meta",
            {},
        )
    )

    removed_lessons: list[core.Lesson] = []
    removed_keys: set[str] = set()
    kept_events: list[dict] = []

    for raw in copied.get(
        "events",
        [],
    ):
        try:
            lesson = core.lesson_from_dict(
                raw
            )
        except Exception:
            kept_events.append(
                raw
            )
            continue

        meta = old_meta.get(
            lesson.exact_key,
            fallback_meta(lesson),
        )

        if lesson_is_completed(
            lesson,
            meta,
            completed,
        ):
            removed_lessons.append(
                lesson
            )

            removed_keys.add(
                lesson.exact_key
            )

            continue

        kept_events.append(
            raw
        )

    copied["events"] = kept_events

    calendar_events = dict(
        copied.get(
            "calendar_events",
            {},
        )
    )

    for key in removed_keys:
        calendar_events.pop(
            key,
            None,
        )

        old_meta.pop(
            key,
            None,
        )

    copied[
        "calendar_events"
    ] = calendar_events

    copied[
        "lesson_meta"
    ] = old_meta

    pending = dict(
        copied.get(
            "pending_missing",
            {},
        )
    )

    for key in removed_keys:
        pending.pop(
            key,
            None,
        )

    copied[
        "pending_missing"
    ] = pending

    clean_history = []

    for item in copied.get(
        "removed_history",
        [],
    ):
        try:
            lesson = core.lesson_from_dict(
                item["lesson"]
            )

            meta = item.get(
                "meta",
                fallback_meta(lesson),
            )

        except Exception:
            clean_history.append(
                item
            )
            continue

        if lesson_is_completed(
            lesson,
            meta,
            completed,
        ):
            continue

        clean_history.append(
            item
        )

    copied[
        "removed_history"
    ] = clean_history

    anti_skip = dict(
        copied.get(
            "anti_skip",
            {},
        )
    )

    for name in (
        "acknowledged",
        "snoozes",
    ):
        values = anti_skip.get(
            name,
            {},
        )

        if not isinstance(
            values,
            dict,
        ):
            continue

        values = dict(
            values
        )

        for key in removed_keys:
            values.pop(
                key,
                None,
            )

        anti_skip[name] = values

    copied[
        "anti_skip"
    ] = anti_skip

    reminded = set(
        copied.get(
            "reminded",
            [],
        )
    )

    for lesson in removed_lessons:
        reminded.discard(
            lesson.reminder_key
        )

        for threshold in legacy.TELEGRAM_REMINDER_MINUTES:
            reminded.discard(
                legacy.reminder_state_key(
                    lesson,
                    threshold,
                )
            )

        for suffix in (
            "day-before",
            "same-day",
            "just-started",
        ):
            reminded.discard(
                legacy.anti_skip_state_key(
                    lesson,
                    suffix,
                )
            )

    if removed_lessons:
        reminded.discard(
            legacy.LIVE_STATE_KEY
        )

        for threshold in legacy.LIVE_FOLLOW_UP_MINUTES:
            reminded.discard(
                f"{legacy.LIVE_STATE_KEY}-follow-up-{threshold}"
            )

    copied[
        "reminded"
    ] = sorted(
        reminded
    )

    return copied


def filter_snapshot_for_completed(
    lessons: list[core.Lesson],
    meta_by_key: dict[str, dict],
    live_present: bool | None,
    live_evidence: dict,
) -> tuple[
    list[core.Lesson],
    dict[str, dict],
    bool | None,
    dict,
]:
    completed = set(
        _ACTIVE_COMPLETED_SUBJECTS
    )

    if not completed:
        return (
            lessons,
            meta_by_key,
            live_present,
            live_evidence,
        )

    normalized_meta: dict[str, dict] = {}

    for lesson in lessons:
        normalized_meta[
            lesson.exact_key
        ] = normalize_lesson_meta(
            lesson,
            meta_by_key.get(
                lesson.exact_key,
                fallback_meta(lesson),
            ),
        )

    kept_lessons: list[core.Lesson] = []
    kept_meta: dict[str, dict] = {}

    for lesson in lessons:
        meta = normalized_meta[
            lesson.exact_key
        ]

        if lesson_is_completed(
            lesson,
            meta,
            completed,
        ):
            continue

        kept_lessons.append(
            lesson
        )

        kept_meta[
            lesson.exact_key
        ] = meta

    # Se la scheda In corso identifica precisamente una materia
    # completata, non deve generare alcun Anti-Salto.
    if live_present is True:
        matched = legacy.match_live_identity_lesson(
            lessons,
            normalized_meta,
            {},
            live_evidence,
        )

        if matched is not None:
            matched_meta = normalized_meta.get(
                matched.exact_key,
                fallback_meta(matched),
            )

            if lesson_is_completed(
                matched,
                matched_meta,
                completed,
            ):
                live_present = False
                live_evidence = {}

    # Fallback prudente solo se nel testo è riconoscibile
    # una sola materia ufficiale.
    if (
        live_present is True
        and isinstance(
            live_evidence,
            dict,
        )
    ):
        evidence_text = _subject_match_text(
            str(
                live_evidence.get(
                    "text",
                    "",
                )
            )
        )

        visible_subjects = {
            subject
            for subject in OFFICIAL_MERCATORUM_SUBJECTS
            if (
                _subject_match_text(subject)
                in evidence_text
            )
        }

        if len(
            visible_subjects
        ) == 1:
            visible = next(
                iter(
                    visible_subjects
                )
            )

            if (
                _subject_match_text(visible)
                in completed_subject_keys(
                    completed
                )
            ):
                live_present = False
                live_evidence = {}

    return (
        kept_lessons,
        kept_meta,
        live_present,
        live_evidence,
    )


def scrape_snapshot_filtered():
    (
        lessons,
        meta_by_key,
        live_present,
        live_evidence,
    ) = _legacy_scrape_snapshot_v32()

    return filter_snapshot_for_completed(
        lessons,
        meta_by_key,
        live_present,
        live_evidence,
    )


def purge_calendar_for_completed(
    completed: set[str],
    control: dict,
) -> dict:
    """
    Una materia viene cancellata dal Calendar soltanto quando
    il suo completamento era già persistito nel run precedente.

    Così non cancelliamo eventi sulla base di uno stato che
    potrebbe non essere ancora stato salvato in state.enc.
    """
    purged = {
        canonical_subject(subject)
        for subject in control.get(
            "calendar_purged_subjects",
            [],
        )
        if canonical_subject(subject)
    }

    pending = {
        canonical_subject(subject)
        for subject in completed
        if canonical_subject(subject)
    } - purged

    if not pending:
        return control

    service, calendar_id = (
        core.google_calendar_service()
    )

    if service is None:
        return control

    try:
        managed_by_id, _ = (
            core.calendar_managed_event_index(
                service,
                calendar_id,
            )
        )

    except Exception as exc:
        print(
            "Pulizia Calendar materie completate non disponibile: "
            f"{type(exc).__name__}"
        )

        return control

    successful = set(
        pending
    )

    for event in managed_by_id.values():
        summary = core.normalize_space(
            str(
                event.get(
                    "summary",
                    "",
                )
            )
        )

        prefix = "🎓 Mercatorum ·"

        if summary.startswith(
            prefix
        ):
            summary = core.normalize_space(
                summary[
                    len(prefix):
                ]
            )

        split = split_official_subject(
            summary
        )

        if split is None:
            continue

        subject = canonical_subject(
            split[0]
        )

        if subject not in pending:
            continue

        event_id = str(
            event.get(
                "id",
                "",
            )
        ).strip()

        if not event_id:
            continue

        try:
            core.calendar_delete(
                service,
                calendar_id,
                event_id,
            )

        except Exception as exc:
            print(
                "Cancellazione Calendar materia completata non riuscita: "
                f"{type(exc).__name__}"
            )

            successful.discard(
                subject
            )

    purged.update(
        successful
    )

    control[
        "calendar_purged_subjects"
    ] = sorted(
        purged,
        key=str.casefold,
    )

    return control


def main_v32() -> int:
    """
    Ordine di sicurezza:

    1. legge state.enc;
    2. controlla periodicamente Punti bonus;
    3. conferma 1/1 per due letture separate;
    4. filtra subito la materia da Programmate/Anti-Salto;
    5. una materia già completata nel run precedente
       viene rimossa dal Calendar;
    6. il main legacy continua normalmente.
    """
    global _ACTIVE_COMPLETED_SUBJECTS

    state = _core_load_state_v32()

    state = legacy.json.loads(
        legacy.json.dumps(
            state,
            ensure_ascii=False,
        )
    )

    now = datetime.now(
        core.TIMEZONE
    )

    old_control = progress_control_from_state(
        state
    )

    previously_completed = {
        canonical_subject(subject)
        for subject in old_control.get(
            "completed_subjects",
            [],
        )
        if canonical_subject(subject)
    }

    progress = None

    if progress_check_is_due(
        old_control,
        now,
    ):
        progress = (
            scrape_progress_bonus_with_retry()
        )

    completed = set(
        previously_completed
    )

    newly_completed: set[str] = set()

    if progress is not None:
        (
            state,
            completed,
            newly_completed,
        ) = update_progress_control(
            state,
            progress,
            now,
        )

    # Pulizia Calendar solo per completamenti già persistiti
    # prima di questo run.
    control = progress_control_from_state(
        state
    )

    if previously_completed:
        control = purge_calendar_for_completed(
            previously_completed,
            control,
        )

        anti_skip = dict(
            state.get(
                "anti_skip",
                {},
            )
        )

        anti_skip[
            PROGRESS_BONUS_STATE_KEY
        ] = control

        state[
            "anti_skip"
        ] = anti_skip

    _ACTIVE_COMPLETED_SUBJECTS = set(
        completed
    )

    filtered_state = filter_state_for_completed(
        state,
        completed,
    )

    load_calls = {
        "count": 0,
    }

    def load_state_v32():
        if load_calls[
            "count"
        ] == 0:
            load_calls[
                "count"
            ] = 1

            return filtered_state

        return _core_load_state_v32()

    core.load_state = load_state_v32

    try:
        result = legacy.main()

    finally:
        core.load_state = (
            _core_load_state_v32
        )

    if newly_completed:
        print(
            "Punti bonus: nuova materia completata confermata; "
            "Anti-Salto disattivato per la relativa materia."
        )

    return result




# ---------------------------------------------------------------------------
# Attiva le sostituzioni nel modulo legacy.
# La logica di monitoraggio rimane quella già collaudata in monitor_runner.py.
# ---------------------------------------------------------------------------

legacy.callback_keyboard = callback_keyboard
legacy.process_telegram_updates = process_telegram_updates
legacy.fallback_meta = fallback_meta
legacy.lesson_card = lesson_card
legacy.modified_card = modified_card
legacy.recovery_card = recovery_card
legacy.notify_recovery_anti_skip = notify_recovery_anti_skip
legacy.scrape_snapshot = scrape_snapshot_filtered


if __name__ == "__main__":
    raise SystemExit(
        main_v32()
    )
