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
# Calendar UI v3.3 - Materia / Lezione
# ---------------------------------------------------------------------------

CALENDAR_LABELS_STATE_KEY = (
    "calendar_subject_title_v1"
)

_core_calendar_body_v33 = (
    core.calendar_body
)

_legacy_ensure_calendar_safety_reminders_v33 = (
    legacy.ensure_calendar_safety_reminders
)


def calendar_body_v33(
    lesson: core.Lesson,
) -> dict:
    """
    Mantiene orari, reminder ed extendedProperties del
    Calendar originale, modificando soltanto il modo in
    cui Materia e Lezione vengono presentate.
    """
    body = _core_calendar_body_v33(
        lesson
    )

    meta = normalize_lesson_meta(
        lesson,
        fallback_meta(lesson),
    )

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

    if not title:
        title = legacy.lesson_name(
            lesson
        )

    if subject:
        summary = (
            "🎓 Mercatorum · "
            f"{subject}"
        )

        if title:
            summary += (
                " · 📘 "
                f"{title}"
            )

        description_lines = [
            f"🎓 Materia: {subject}",
        ]

        if title:
            description_lines.append(
                f"📘 Lezione: {title}"
            )

        description_lines += [
            "",
            "Didattica sincrona Mercatorum.",
            (
                "Evento creato e mantenuto automaticamente "
                "da Mercatorum Sync Monitor."
            ),
        ]

        body[
            "description"
        ] = "\n".join(
            description_lines
        )

    if not subject:
        summary = (
            "🎓 Mercatorum · "
            f"📘 {title}"
        )

        body[
            "description"
        ] = (
            f"📘 Lezione: {title}\n\n"
            "Didattica sincrona Mercatorum.\n"
            "Evento creato e mantenuto automaticamente "
            "da Mercatorum Sync Monitor."
        )

    # Evita titoli Calendar eccessivamente lunghi.
    # La descrizione conserva comunque il testo completo.
    if len(summary) > 180:
        summary = (
            summary[:177]
            + "…"
        )

    body[
        "summary"
    ] = summary

    return body


def ensure_calendar_safety_reminders_v33(
    lessons: list[core.Lesson],
    calendar_events: dict[str, str],
    anti_skip_state: dict,
) -> None:
    """
    Conserva la migrazione reminder già esistente e,
    una sola volta, aggiorna gli eventi Calendar esistenti
    al formato Materia / Lezione.

    I futuri eventi useranno automaticamente calendar_body_v33.
    """
    _legacy_ensure_calendar_safety_reminders_v33(
        lessons,
        calendar_events,
        anti_skip_state,
    )

    if anti_skip_state.get(
        CALENDAR_LABELS_STATE_KEY
    ):
        return

    service, calendar_id = (
        core.google_calendar_service()
    )

    if service is None:
        return

    complete = True
    updated = 0

    for lesson in lessons:
        event_id = calendar_events.get(
            lesson.exact_key
        )

        if not event_id:
            continue

        try:
            core.calendar_update(
                service,
                calendar_id,
                event_id,
                lesson,
            )

            updated += 1

        except Exception as exc:
            print(
                "Aggiornamento etichette Calendar "
                "non riuscito: "
                f"{type(exc).__name__}"
            )

            complete = False

    if not complete:
        return

    anti_skip_state[
        CALENDAR_LABELS_STATE_KEY
    ] = True

    if updated:
        print(
            "Calendar: aggiornate etichette "
            "Materia/Lezione su "
            f"{updated} eventi."
        )





# ---------------------------------------------------------------------------
# Matching v3.4 - Materia + Lezione rigorosamente identiche
# ---------------------------------------------------------------------------

STRICT_IDENTITY_MATCHING_V34 = True


def strict_identity_text(
    value: str,
) -> str:
    """
    Normalizzazione esclusivamente cosmetica.

    Sono equivalenti:
    - maiuscole/minuscole;
    - spazi multipli;
    - apostrofo normale, tipografico e backtick.

    NON eliminiamo parole e NON usiamo percentuali di similarità.
    """
    return (
        core.normalize_space(
            str(value or "")
        )
        .casefold()
        .replace("’", "'")
        .replace("`", "'")
    )


def strict_identity_from_meta(
    lesson: core.Lesson,
    meta: dict | None = None,
) -> tuple[str, str] | None:
    clean_meta = normalize_lesson_meta(
        lesson,
        (
            meta
            or fallback_meta(lesson)
        ),
    )

    subject = strict_identity_text(
        clean_meta.get(
            "subject",
            "",
        )
    )

    title = strict_identity_text(
        clean_meta.get(
            "title",
            "",
        )
    )

    # Per dichiarare che due didattiche sono la stessa
    # pretendiamo ENTRAMBI i dati.
    if not subject:
        return None

    if not title:
        return None

    return (
        subject,
        title,
    )


def strict_diff_lessons(
    old: list[core.Lesson],
    new: list[core.Lesson],
):
    """
    Sostituisce il vecchio matching >=80%.

    Una didattica viene considerata modificata/spostata
    soltanto quando Materia E Titolo coincidono esattamente
    dopo la normalizzazione cosmetica.

    In caso di ambiguità tra più occorrenze identiche,
    non indoviniamo: restano aggiunte/rimosse separate.
    """
    old_by_exact = {
        item.exact_key: item
        for item in old
    }

    new_by_exact = {
        item.exact_key: item
        for item in new
    }

    removed = [
        lesson
        for key, lesson in old_by_exact.items()
        if key not in new_by_exact
    ]

    added = [
        lesson
        for key, lesson in new_by_exact.items()
        if key not in old_by_exact
    ]

    old_groups: dict[
        tuple[str, str],
        list[core.Lesson],
    ] = {}

    new_groups: dict[
        tuple[str, str],
        list[core.Lesson],
    ] = {}

    for lesson in removed:
        identity = strict_identity_from_meta(
            lesson
        )

        if identity is None:
            continue

        old_groups.setdefault(
            identity,
            [],
        ).append(
            lesson
        )

    for lesson in added:
        identity = strict_identity_from_meta(
            lesson
        )

        if identity is None:
            continue

        new_groups.setdefault(
            identity,
            [],
        ).append(
            lesson
        )

    modified: list[
        tuple[
            core.Lesson,
            core.Lesson,
        ]
    ] = []

    paired_old: set[str] = set()
    paired_new: set[str] = set()

    for identity in (
        set(old_groups)
        & set(new_groups)
    ):
        old_candidates = old_groups[
            identity
        ]

        new_candidates = new_groups[
            identity
        ]

        # Sicurezza anti-errore:
        # se ci sono più possibili corrispondenze
        # con stesso titolo non scegliamo a caso.
        if len(old_candidates) != 1:
            continue

        if len(new_candidates) != 1:
            continue

        before = old_candidates[0]
        after = new_candidates[0]

        modified.append(
            (
                before,
                after,
            )
        )

        paired_old.add(
            before.exact_key
        )

        paired_new.add(
            after.exact_key
        )

    remaining_removed = [
        lesson
        for lesson in removed
        if lesson.exact_key
        not in paired_old
    ]

    remaining_added = [
        lesson
        for lesson in added
        if lesson.exact_key
        not in paired_new
    ]

    modified.sort(
        key=lambda pair: (
            pair[1].start_dt,
            pair[1].end_dt,
            pair[1].description.casefold(),
        )
    )

    return (
        remaining_added,
        remaining_removed,
        modified,
    )


def recovery_score_strict(
    new_lesson: core.Lesson,
    new_meta: dict,
    item: dict,
) -> tuple[float, bool]:
    """
    Anche un recupero dalla cronologia è valido SOLO
    con stessa Materia + stesso Titolo.

    Lo stesso mercatorum_id aumenta la certezza,
    ma NON può mai aggirare il controllo identità.
    """
    old_lesson = core.lesson_from_dict(
        item["lesson"]
    )

    old_meta = item.get(
        "meta",
        fallback_meta(
            old_lesson
        ),
    )

    new_identity = (
        strict_identity_from_meta(
            new_lesson,
            new_meta,
        )
    )

    old_identity = (
        strict_identity_from_meta(
            old_lesson,
            old_meta,
        )
    )

    if new_identity is None:
        return (
            0.0,
            False,
        )

    if old_identity is None:
        return (
            0.0,
            False,
        )

    if new_identity != old_identity:
        return (
            0.0,
            False,
        )

    clean_new_meta = normalize_lesson_meta(
        new_lesson,
        new_meta,
    )

    clean_old_meta = normalize_lesson_meta(
        old_lesson,
        old_meta,
    )

    new_id = core.normalize_space(
        clean_new_meta.get(
            "mercatorum_id",
            "",
        )
    )

    old_id = core.normalize_space(
        clean_old_meta.get(
            "mercatorum_id",
            "",
        )
    )

    if (
        new_id
        and old_id
        and new_id == old_id
    ):
        return (
            2.0,
            True,
        )

    return (
        1.0,
        False,
    )


def find_recovery_strict(
    lesson: core.Lesson,
    meta: dict,
    history: list[dict],
) -> tuple[int | None, bool]:
    """
    Ordine:

    1. Materia+Titolo devono essere identici.
    2. Se c'è lo stesso ID Mercatorum, match certo.
    3. Se ricompare con identico giorno/orario,
       preferiamo quell'occorrenza -> RIPRISTINATA.
    4. Se resta una sola possibile vecchia occorrenza,
       è una RIPROGRAMMATA.
    5. Se ne restano più di una, non indoviniamo.
    """
    matches: list[
        tuple[
            int,
            core.Lesson,
            bool,
        ]
    ] = []

    for index in range(
        len(history) - 1,
        -1,
        -1,
    ):
        score, certain = (
            recovery_score_strict(
                lesson,
                meta,
                history[index],
            )
        )

        if score < 1.0:
            continue

        try:
            old_lesson = (
                core.lesson_from_dict(
                    history[index][
                        "lesson"
                    ]
                )
            )
        except Exception:
            continue

        matches.append(
            (
                index,
                old_lesson,
                certain,
            )
        )

    if not matches:
        return (
            None,
            False,
        )

    certain_matches = [
        item
        for item in matches
        if item[2]
    ]

    if len(certain_matches) == 1:
        return (
            certain_matches[0][0],
            True,
        )

    if len(certain_matches) > 1:
        return (
            None,
            False,
        )

    same_schedule = [
        item
        for item in matches
        if (
            item[1].date
            == lesson.date
            and item[1].start
            == lesson.start
            and item[1].end
            == lesson.end
        )
    ]

    if len(same_schedule) == 1:
        return (
            same_schedule[0][0],
            False,
        )

    if len(same_schedule) > 1:
        return (
            None,
            False,
        )

    if len(matches) == 1:
        return (
            matches[0][0],
            False,
        )

    # Più vecchie didattiche con identica materia/titolo
    # e nessun elemento univoco: meglio nuova didattica
    # che falsa riprogrammazione.
    return (
        None,
        False,
    )


def recovery_card(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
) -> str:
    """
    Con il matching V3.4 sappiamo già che materia e titolo
    sono identici.

    Stessa programmazione  -> RIPRISTINATA
    Programmazione diversa -> RIPROGRAMMATA
    """
    old_meta = normalize_lesson_meta(
        old_lesson,
        old_meta,
    )

    new_meta = normalize_lesson_meta(
        new_lesson,
        new_meta,
    )

    same_schedule = (
        old_lesson.date
        == new_lesson.date
        and old_lesson.start
        == new_lesson.start
        and old_lesson.end
        == new_lesson.end
    )

    header = (
        "♻️ DIDATTICA RIPRISTINATA"
        if same_schedule
        else "♻️ DIDATTICA RIPROGRAMMATA"
    )

    lines = [
        header,
        "",
        *lesson_identity_lines(
            new_lesson,
            new_meta,
        ),
        "",
    ]

    if same_schedule:
        lines += (
            lesson_schedule_lines(
                new_lesson
            )
        )

    if not same_schedule:
        lines += [
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

    return "\n".join(
        lines
    )




# ---------------------------------------------------------------------------
# Post-lezione v3.5 - Terminate / In aggiornamento / Test disponibile
# ---------------------------------------------------------------------------

POST_LESSON_STATE_KEY = "post_lesson_v35"
POST_LESSON_RETENTION_DAYS = max(
    4,
    int(
        os.getenv(
            "POST_LESSON_RETENTION_DAYS",
            "7",
        )
    ),
)
POST_LESSON_ARM_PAST_HOURS = max(
    1,
    int(
        os.getenv(
            "POST_LESSON_ARM_PAST_HOURS",
            "6",
        )
    ),
)
POST_LESSON_SCRAPE_ATTEMPTS = max(
    1,
    int(
        os.getenv(
            "POST_LESSON_SCRAPE_ATTEMPTS",
            "3",
        )
    ),
)
POST_LESSON_RETRY_WAIT_SECONDS = max(
    0,
    int(
        os.getenv(
            "POST_LESSON_RETRY_WAIT_SECONDS",
            "8",
        )
    ),
)

_legacy_save_extended_state_v35 = (
    legacy.save_extended_state
)


def post_lesson_tracking_key(
    lesson: core.Lesson,
    meta: dict,
) -> str | None:
    identity = strict_identity_from_meta(
        lesson,
        meta,
    )

    if identity is None:
        return None

    raw = "|".join(
        [
            lesson.date,
            lesson.start,
            lesson.end,
            identity[0],
            identity[1],
        ]
    )

    return legacy.hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def post_lesson_control(
    anti_skip_state: dict,
) -> dict:
    raw = anti_skip_state.get(
        POST_LESSON_STATE_KEY,
        {},
    )

    return (
        dict(raw)
        if isinstance(raw, dict)
        else {}
    )


def tracker_lesson_and_meta(
    entry: dict,
) -> tuple[core.Lesson, dict] | None:
    try:
        lesson = core.lesson_from_dict(
            entry["lesson"]
        )

        meta = normalize_lesson_meta(
            lesson,
            entry.get(
                "meta",
                fallback_meta(lesson),
            ),
        )

        if strict_identity_from_meta(
            lesson,
            meta,
        ) is None:
            return None

        return lesson, meta

    except Exception:
        return None


def arm_post_lesson_tracking(
    lessons: list[core.Lesson],
    lesson_meta: dict[str, dict],
    anti_skip_state: dict,
    now: datetime,
) -> dict:
    control = post_lesson_control(
        anti_skip_state
    )

    tracked_raw = control.get(
        "tracked",
        {},
    )

    tracked = (
        dict(tracked_raw)
        if isinstance(tracked_raw, dict)
        else {}
    )

    active_keys: set[str] = set()
    arm_cutoff = now - timedelta(
        hours=POST_LESSON_ARM_PAST_HOURS
    )

    for lesson in lessons:
        if lesson.end_dt < arm_cutoff:
            continue

        meta = normalize_lesson_meta(
            lesson,
            lesson_meta.get(
                lesson.exact_key,
                fallback_meta(lesson),
            ),
        )

        if lesson_is_completed(
            lesson,
            meta,
            set(_ACTIVE_COMPLETED_SUBJECTS),
        ):
            continue

        key = post_lesson_tracking_key(
            lesson,
            meta,
        )

        if key is None:
            continue

        active_keys.add(key)

        existing = tracked.get(
            key,
            {},
        )

        if not isinstance(existing, dict):
            existing = {}

        entry = dict(existing)
        entry["lesson"] = asdict(lesson)
        entry["meta"] = {
            "subject": core.normalize_space(
                meta.get("subject", "")
            ),
            "title": core.normalize_space(
                meta.get("title", "")
            ),
        }

        if not entry.get("armed_at"):
            entry["armed_at"] = now.isoformat()

        tracked[key] = entry

    retention_cutoff = now - timedelta(
        days=POST_LESSON_RETENTION_DAYS
    )

    for key in list(tracked):
        entry = tracked.get(key)

        if not isinstance(entry, dict):
            tracked.pop(key, None)
            continue

        parsed = tracker_lesson_and_meta(
            entry
        )

        if parsed is None:
            tracked.pop(key, None)
            continue

        lesson, meta = parsed

        if lesson_is_completed(
            lesson,
            meta,
            set(_ACTIVE_COMPLETED_SUBJECTS),
        ):
            tracked.pop(key, None)
            continue

        # Una programmazione futura non più presente nello stato attivo
        # è stata rimossa/riprogrammata: non deve generare post-lezione.
        if (
            key not in active_keys
            and lesson.start_dt > now
            and not entry.get(
                "terminated_notified_at"
            )
        ):
            tracked.pop(key, None)
            continue

        # Evita crescita indefinita dello stato. Sette giorni coprono
        # ampiamente la finestra test di 72 ore senza inventare scadenze.
        if lesson.end_dt < retention_cutoff:
            tracked.pop(key, None)

    control["tracked"] = tracked
    control["version"] = 1
    anti_skip_state[
        POST_LESSON_STATE_KEY
    ] = control

    return control


def terminated_meta_from_tail_v35(
    tail: str,
) -> dict | None:
    split = split_official_subject(
        tail
    )

    if split is None:
        return None

    subject, remainder = split

    presence = re.search(
        r"%\s*presenza\b",
        remainder,
        flags=re.IGNORECASE,
    )

    if presence is None:
        return None

    before_presence = remainder[
        :presence.start()
    ]

    test_labels = list(
        re.finditer(
            r"\bTest\b",
            before_presence,
            flags=re.IGNORECASE,
        )
    )

    if not test_labels:
        return None

    # La colonna Test è l'ultima occorrenza di "Test" prima di % presenza.
    # Questo evita di tagliare un eventuale "test" presente nel titolo.
    title = core.normalize_space(
        before_presence[
            :test_labels[-1].start()
        ]
    )

    # "In aggiornamento" è stato osservato come stato della riga,
    # non come parte dell'identità della lezione.
    updating = re.search(
        r"\bIn\s+aggiornamento\b",
        title,
        flags=re.IGNORECASE,
    )

    if updating is not None:
        title = core.normalize_space(
            title[:updating.start()]
        )

    if not subject or not title:
        return None

    return {
        "subject": subject,
        "title": title,
        "mercatorum_id": "",
    }


def terminated_blocks_v35(
    body_text: str,
) -> list[dict]:
    text = core.normalize_space(
        body_text
    )

    matches = list(
        core.DATE_RE.finditer(
            text
        )
    )

    result: list[dict] = []

    for index, match in enumerate(matches):
        block_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        block = text[
            match.start():block_end
        ]

        time_match = core.TIME_RE.search(
            block
        )

        if time_match is None:
            continue

        day = int(
            match.group("day")
        )
        month = core.MONTHS[
            match.group("month").lower()
        ]
        year = int(
            match.group("year")
        )

        date_iso = (
            f"{year:04d}-{month:02d}-{day:02d}"
        )
        start = time_match.group("start")
        end = time_match.group("end")

        tail = core.normalize_space(
            block[time_match.end():]
        )
        meta = terminated_meta_from_tail_v35(
            tail
        )

        lesson = None

        if meta is not None:
            lesson = core.Lesson(
                date=date_iso,
                start=start,
                end=end,
                description=core.normalize_space(
                    f"{meta['subject']} {meta['title']}"
                ),
            )

        low = block.casefold()

        result.append(
            {
                "lesson": lesson,
                "meta": meta,
                "contains_in_aggiornamento": (
                    "in aggiornamento"
                    in low
                ),
                "contains_accedi_al_test": (
                    "accedi al test"
                    in low
                ),
                "contains_test_label": bool(
                    re.search(
                        r"\btest\b",
                        block,
                        re.IGNORECASE,
                    )
                ),
                "contains_presence": bool(
                    re.search(
                        r"%\s*presenza",
                        block,
                        re.IGNORECASE,
                    )
                ),
                "contains_recording": (
                    "registrazione"
                    in low
                ),
            }
        )

    return result


def terminated_test_controls_v35(
    page,
) -> list[dict]:
    return page.evaluate(
        r"""
        () => {
          const norm = value =>
            (value || '')
              .replace(/\s+/g, ' ')
              .trim();

          const all = Array.from(
            document.querySelectorAll('body *')
          );

          const leaves = all.filter(el => {
            const text = norm(
              el.innerText || el.textContent
            );

            if (!/^accedi\s+al\s+test$/i.test(text)) {
              return false;
            }

            const childHasSameText = Array.from(
              el.children
            ).some(child =>
              /^accedi\s+al\s+test$/i.test(
                norm(
                  child.innerText ||
                  child.textContent
                )
              )
            );

            if (childHasSameText) {
              return false;
            }

            const style = window.getComputedStyle(el);

            return (
              style.display !== 'none' &&
              style.visibility !== 'hidden'
            );
          });

          return leaves.map(leaf => {
            const control = leaf.closest(
              [
                'button',
                'a',
                '[role="button"]',
                '[tabindex]'
              ].join(',')
            ) || leaf;

            const style = window.getComputedStyle(
              control
            );
            const ariaDisabled = control.getAttribute(
              'aria-disabled'
            );

            const disabled =
              control.disabled === true ||
              control.matches(':disabled') ||
              control.hasAttribute('disabled') ||
              ariaDisabled === 'true' ||
              style.pointerEvents === 'none';

            const href = control.getAttribute('href');
            const validHref =
              Boolean(href) &&
              href !== '#' &&
              href !== '' &&
              !href.toLowerCase().startsWith(
                'javascript:'
              );

            let active = false;

            if (control.tagName === 'BUTTON') {
              active = !disabled;
            }

            if (control.tagName === 'A') {
              active = !disabled && validHref;
            }

            if (
              control.tagName !== 'BUTTON' &&
              control.tagName !== 'A'
            ) {
              active =
                !disabled &&
                (
                  control.getAttribute('role') ===
                    'button' ||
                  control.hasAttribute('tabindex')
                );
            }

            return {
              tag: control.tagName,
              active: active,
              disabled: disabled,
              valid_href: validHref
            };
          });
        }
        """
    )


def scrape_terminated_v35_once() -> list[dict] | None:
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
            core.settle_spa(page, 2000)
            core.login_if_needed(page)

            page.goto(
                core.SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            core.settle_spa(page, 2000)

            if core.first_visible(
                page,
                [
                    "#password",
                    "input[type='password']",
                ],
            ):
                core.login_if_needed(page)
                page.goto(
                    core.SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                core.settle_spa(page, 2000)

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
                    "[role='tab']:has-text('Terminate')",
                    "button:has-text('Terminate')",
                    "a:has-text('Terminate')",
                    "text=Terminate",
                ],
            )

            if tab is None:
                raise RuntimeError(
                    "Scheda Terminate non trovata."
                )

            tab.click(timeout=10_000)
            core.settle_spa(page, 2000)

            body_text = page.locator(
                "body"
            ).inner_text(timeout=15_000)

            blocks = terminated_blocks_v35(
                body_text
            )
            controls = terminated_test_controls_v35(
                page
            )

            print(
                "V3.5 Terminate: "
                f"{len(blocks)} blocchi / "
                f"{len(controls)} controlli test."
            )

            if (
                not blocks
                or len(blocks) != len(controls)
            ):
                print(
                    "V3.5 Terminate: pairing non affidabile; "
                    "nessuna notifica post-lezione."
                )
                return None

            observations: list[dict] = []

            for index in range(len(blocks)):
                block = dict(blocks[index])
                block["control"] = controls[index]
                observations.append(block)

            core.best_effort_logout(page)
            return observations

        finally:
            context.close()
            browser.close()


def scrape_terminated_v35_with_retry() -> list[dict] | None:
    for attempt in range(
        1,
        POST_LESSON_SCRAPE_ATTEMPTS + 1,
    ):
        try:
            return scrape_terminated_v35_once()

        except Exception as exc:
            print(
                "V3.5 Terminate non disponibile "
                f"({attempt}/{POST_LESSON_SCRAPE_ATTEMPTS}): "
                f"{type(exc).__name__}"
            )

            if (
                attempt < POST_LESSON_SCRAPE_ATTEMPTS
                and POST_LESSON_RETRY_WAIT_SECONDS > 0
            ):
                legacy.time.sleep(
                    POST_LESSON_RETRY_WAIT_SECONDS
                )

    return None


def match_terminated_observation_v35(
    entry: dict,
    observations: list[dict],
) -> dict | None:
    parsed = tracker_lesson_and_meta(
        entry
    )

    if parsed is None:
        return None

    lesson, meta = parsed
    target_identity = strict_identity_from_meta(
        lesson,
        meta,
    )

    if target_identity is None:
        return None

    matches: list[dict] = []

    for observation in observations:
        observed_lesson = observation.get(
            "lesson"
        )
        observed_meta = observation.get(
            "meta"
        )

        if not isinstance(
            observed_lesson,
            core.Lesson,
        ):
            continue

        if not isinstance(
            observed_meta,
            dict,
        ):
            continue

        observed_identity = strict_identity_from_meta(
            observed_lesson,
            observed_meta,
        )

        if observed_identity != target_identity:
            continue

        # Il pairing test->lezione è accettato solo sulla stessa
        # programmazione esatta. Se Mercatorum cambia struttura o dati,
        # meglio nessuna notifica che un test attribuito male.
        if (
            observed_lesson.date != lesson.date
            or observed_lesson.start != lesson.start
            or observed_lesson.end != lesson.end
        ):
            continue

        if not (
            observation.get(
                "contains_accedi_al_test"
            )
            and observation.get(
                "contains_test_label"
            )
            and observation.get(
                "contains_presence"
            )
            and observation.get(
                "contains_recording"
            )
        ):
            continue

        matches.append(observation)

    if len(matches) != 1:
        return None

    return matches[0]


def notify_post_lesson_v35(
    title: str,
    body: str,
) -> bool:
    chat_id = legacy.telegram_main_chat_id()

    if not chat_id:
        return False

    try:
        legacy.telegram_send_html(
            title,
            body,
            chat_id,
        )

        print(
            f"{title} — notifica elaborata "
            "(dettagli omessi dal log)."
        )
        return True

    except Exception as exc:
        print(
            "V3.5 notifica Telegram non riuscita: "
            f"{type(exc).__name__}"
        )
        return False


def post_lesson_body_v35(
    lesson: core.Lesson,
    meta: dict,
    phase: str,
) -> str:
    lines = lesson_identity_lines(
        lesson,
        meta,
    )

    lines.append("")

    if phase == "terminated":
        lines.append(
            "La lezione non è più in diretta."
        )

    elif phase == "updating":
        lines += [
            "Mercatorum sta aggiornando i dati della didattica.",
            "📝 Test: non ancora disponibile",
        ]

    elif phase == "test":
        lines += [
            "📝 Ora puoi accedere al test.",
            "⏳ Il test può essere svolto entro 72 ore.",
        ]

    else:
        raise ValueError(
            "Fase post-lezione non riconosciuta."
        )

    return "\n".join(lines)


def process_post_lesson_v35(
    lessons: list[core.Lesson],
    lesson_meta: dict[str, dict],
    anti_skip_state: dict,
) -> None:
    now = datetime.now(
        core.TIMEZONE
    )

    control = arm_post_lesson_tracking(
        lessons,
        lesson_meta,
        anti_skip_state,
        now,
    )

    tracked = control.get(
        "tracked",
        {},
    )

    if not isinstance(tracked, dict):
        return

    eligible: list[tuple[str, dict]] = []

    for key, entry in tracked.items():
        if not isinstance(entry, dict):
            continue

        parsed = tracker_lesson_and_meta(
            entry
        )

        if parsed is None:
            continue

        lesson, _ = parsed

        if lesson.start_dt > now:
            continue

        if (
            now - lesson.end_dt
            > timedelta(
                days=POST_LESSON_RETENTION_DAYS
            )
        ):
            continue

        if entry.get(
            "test_available_notified_at"
        ):
            continue

        eligible.append(
            (key, entry)
        )

    if not eligible:
        return

    observations = (
        scrape_terminated_v35_with_retry()
    )

    if observations is None:
        return

    for key, entry in eligible:
        match = match_terminated_observation_v35(
            entry,
            observations,
        )

        if match is None:
            continue

        parsed = tracker_lesson_and_meta(
            entry
        )

        if parsed is None:
            continue

        lesson, meta = parsed

        if not entry.get(
            "terminated_notified_at"
        ):
            sent = notify_post_lesson_v35(
                "🏁 DIDATTICA TERMINATA",
                post_lesson_body_v35(
                    lesson,
                    meta,
                    "terminated",
                ),
            )

            if not sent:
                # Preserva l'ordine delle fasi: se la prima notifica
                # fallisce, le successive aspettano il run seguente.
                continue

            entry[
                "terminated_notified_at"
            ] = now.isoformat()

        if (
            match.get(
                "contains_in_aggiornamento"
            )
            and not entry.get(
                "updating_notified_at"
            )
        ):
            sent = notify_post_lesson_v35(
                "⏳ AGGIORNAMENTO POST-LEZIONE",
                post_lesson_body_v35(
                    lesson,
                    meta,
                    "updating",
                ),
            )

            if sent:
                entry[
                    "updating_notified_at"
                ] = now.isoformat()

        control_info = match.get(
            "control",
            {},
        )

        if (
            not match.get(
                "contains_in_aggiornamento"
            )
            and isinstance(control_info, dict)
            and control_info.get("active") is True
            and not entry.get(
                "test_available_notified_at"
            )
        ):
            sent = notify_post_lesson_v35(
                "✅ TEST DISPONIBILE",
                post_lesson_body_v35(
                    lesson,
                    meta,
                    "test",
                ),
            )

            if sent:
                entry[
                    "test_available_notified_at"
                ] = now.isoformat()

        tracked[key] = entry

    control["tracked"] = tracked
    control["last_success_at"] = now.isoformat()
    anti_skip_state[
        POST_LESSON_STATE_KEY
    ] = control


def save_extended_state_v35(
    lessons: list[core.Lesson],
    reminded: set[str],
    calendar_events: dict[str, str],
    pending_missing: dict[str, dict],
    removed_history: list[dict],
    lesson_meta: dict[str, dict],
    anti_skip_state: dict,
) -> None:
    """
    V3.5 è additiva: lavora solo sul proprio stato dentro anti_skip
    e poi delega integralmente il salvataggio cifrato alla V3 esistente.
    Un errore post-lezione non deve bloccare Calendar/Anti-Salto/state.enc.
    """
    try:
        process_post_lesson_v35(
            lessons,
            lesson_meta,
            anti_skip_state,
        )

    except Exception as exc:
        print(
            "V3.5 post-lezione non disponibile: "
            f"{type(exc).__name__}"
        )

    _legacy_save_extended_state_v35(
        lessons,
        reminded,
        calendar_events,
        pending_missing,
        removed_history,
        lesson_meta,
        anti_skip_state,
    )

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
legacy.save_extended_state = save_extended_state_v35

# V3.4: nessun matching per semplice similarità percentuale.
core.diff_lessons = strict_diff_lessons
legacy.recovery_score = recovery_score_strict
legacy.find_recovery = find_recovery_strict

core.calendar_body = calendar_body_v33
legacy.ensure_calendar_safety_reminders = ensure_calendar_safety_reminders_v33


if __name__ == "__main__":
    raise SystemExit(
        main_v32()
    )
