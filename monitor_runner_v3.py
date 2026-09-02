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


if __name__ == "__main__":
    raise SystemExit(
        legacy.main()
    )
