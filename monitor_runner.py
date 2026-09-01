from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from playwright.sync_api import sync_playwright

import monitor as core


EMPTY_LIVE_TEXT = "nessuna lezione in diretta"
LIVE_TRANSITION_GRACE_MINUTES = 10
MISSING_CONFIRM_CHECKS = int(os.getenv("MISSING_CONFIRM_CHECKS", "3"))
REMOVED_HISTORY_DAYS = int(os.getenv("REMOVED_HISTORY_DAYS", "365"))
LIVE_STATE_KEY = hashlib.sha1(b"mercatorum-live-present").hexdigest()[:16]
SNAPSHOT_ATTEMPTS = max(
    1,
    int(os.getenv("SNAPSHOT_ATTEMPTS", "3")),
)
SNAPSHOT_RETRY_WAIT_SECONDS = max(
    0,
    int(os.getenv("SNAPSHOT_RETRY_WAIT_SECONDS", "8")),
)

# Anti-Salto v1.
MANDATORY_TELEGRAM_REMINDERS = {120, 45, 15, 5}
DAY_BEFORE_HOUR = int(os.getenv("ANTI_SKIP_DAY_BEFORE_HOUR", "19"))
SAME_DAY_HOUR = int(os.getenv("ANTI_SKIP_SAME_DAY_HOUR", "8"))
JUST_STARTED_GRACE_MINUTES = int(
    os.getenv("ANTI_SKIP_JUST_STARTED_GRACE_MINUTES", "12")
)
LIVE_FOLLOW_UP_MINUTES = (10, 20)

# Un segnale "In corso" non associabile a una lezione programmata deve
# sopravvivere a due snapshot separati prima di generare un alert.
GENERIC_LIVE_CONFIRM_CHECKS = max(
    2,
    int(os.getenv("GENERIC_LIVE_CONFIRM_CHECKS", "2")),
)
GENERIC_LIVE_MAX_GAP_MINUTES = max(
    5,
    int(os.getenv("GENERIC_LIVE_MAX_GAP_MINUTES", "12")),
)

# Anti-Salto v2: interazioni Telegram.
SNOOZE_MINUTES = int(os.getenv("ANTI_SKIP_SNOOZE_MINUTES", "15"))
ACK_RECENT_HOURS = int(os.getenv("ANTI_SKIP_ACK_RECENT_HOURS", "24"))
ACK_KEEP_REMINDERS = {45, 5}
CALLBACK_PREFIX = "as2"

# Seconda rete di sicurezza indipendente da Telegram.
CALENDAR_SAFETY_REMINDERS = [1440, 120, 30, 10]
core.CALENDAR_REMINDERS = CALENDAR_SAFETY_REMINDERS

WEEKDAY_RE = re.compile(
    r"^(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)$",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
YEAR_RE = re.compile(r"^20\d{2}$")

FOOTER_MARKERS = (
    "università mercatorum",
    "universita mercatorum",
    "questo sito utilizza i cookie",
    "maggiori info",
    "accetta",
)


def has_footer_noise(value: str) -> bool:
    low = core.normalize_space(value).casefold()
    return any(marker in low for marker in FOOTER_MARKERS)


def is_dirty_lesson(lesson: core.Lesson, meta: dict | None = None) -> bool:
    meta = meta or {}
    values = [
        lesson.description,
        meta.get("subject", ""),
        meta.get("title", ""),
    ]
    return any(has_footer_noise(value) for value in values if value)


def parse_telegram_reminder_minutes() -> list[int]:
    raw = os.getenv("TELEGRAM_REMINDER_MINUTES", "120,45,15,5")
    values: set[int] = set(MANDATORY_TELEGRAM_REMINDERS)

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError(
                "TELEGRAM_REMINDER_MINUTES deve contenere minuti interi "
                "separati da virgola, es. 120,45,15,5"
            ) from exc
        if value > 0:
            values.add(value)

    return sorted(values, reverse=True)


TELEGRAM_REMINDER_MINUTES = parse_telegram_reminder_minutes()


def telegram_html(value: str) -> str:
    """
    Converte solo il grassetto **testo** in HTML Telegram e fa escape
    di tutto il resto, così titoli/materie con &, < o > restano sicuri.
    """
    escaped = html.escape(value)
    return re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", escaped)


def telegram_api(method: str, payload: dict | None = None) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN non configurato.")

    data = json.dumps(
        payload or {},
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8")

    result = json.loads(raw)
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API {method} non riuscita: "
            f"{result.get('description', 'errore sconosciuto')}"
        )
    return result


def telegram_main_chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_urgent_chat_id(
    anti_skip_state: dict | None = None,
) -> str:
    """
    Modalità group-only: gli alert urgenti non hanno una seconda
    destinazione privata.
    """
    return ""

def telegram_owner_user_id(
    anti_skip_state: dict | None = None,
) -> str:
    """
    Modalità group-only: nessun utente privato proprietario.
    I callback autorizzati vengono controllati tramite la chat di gruppo.
    """
    return ""

def telegram_allowed_callback_chats(
    anti_skip_state: dict,
) -> set[str]:
    main_chat = telegram_main_chat_id()
    return {main_chat} if main_chat else set()

def telegram_send_html(
    title: str,
    body: str,
    chat_id: str,
    reply_markup: dict | None = None,
) -> None:
    if not chat_id:
        return

    rendered_title = f"<b>{html.escape(title)}</b>"
    rendered_body = telegram_html(body)
    text = f"{rendered_title}\n\n{rendered_body}"

    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    telegram_api("sendMessage", payload)


def telegram_notify_rich(title: str, body: str) -> None:
    chat_id = telegram_main_chat_id()
    if not chat_id:
        return
    telegram_send_html(title, body, chat_id)


# core.notify continua a funzionare per messaggi non legati a una singola lezione.
core.telegram_notify = telegram_notify_rich


def callback_data(action: str, lesson: core.Lesson) -> str:
    value = f"{CALLBACK_PREFIX}:{action}:{lesson.exact_key}"
    if len(value.encode("utf-8")) > 64:
        raise RuntimeError("Callback Telegram troppo lunga.")
    return value


def callback_keyboard(
    lesson: core.Lesson,
    acknowledged: bool = False,
    snoozed: bool = False,
) -> dict:
    ack_text = "✅ Preso nota ✓" if acknowledged else "✅ Preso nota"
    snooze_text = (
        f"⏰ Tra {SNOOZE_MINUTES} min ✓"
        if snoozed
        else f"⏰ Ricordamelo tra {SNOOZE_MINUTES} min"
    )

    return {
        "inline_keyboard": [
            [
                {
                    "text": ack_text,
                    "callback_data": callback_data("ack", lesson),
                },
            ],
            [
                {
                    "text": snooze_text,
                    "callback_data": callback_data("snooze", lesson),
                },
            ],
            [
                {
                    "text": "▶️ Apri Mercatorum",
                    "url": core.SCHEDULE_URL,
                },
            ],
        ]
    }


def acknowledgement_entry(
    lesson: core.Lesson,
    anti_skip_state: dict,
) -> dict | None:
    acknowledged = anti_skip_state.get("acknowledged", {})
    value = acknowledged.get(lesson.exact_key)
    return value if isinstance(value, dict) else None


def acknowledgement_is_recent(
    lesson: core.Lesson,
    anti_skip_state: dict,
    now: datetime,
) -> bool:
    entry = acknowledgement_entry(lesson, anti_skip_state)
    if not entry:
        return False
    try:
        acknowledged_at = datetime.fromisoformat(entry["at"])
        if acknowledged_at.tzinfo is None:
            acknowledged_at = acknowledged_at.replace(tzinfo=core.TIMEZONE)
    except Exception:
        return False
    return now - acknowledged_at <= timedelta(hours=ACK_RECENT_HOURS)


def snooze_is_active(
    lesson: core.Lesson,
    anti_skip_state: dict,
    now: datetime,
) -> bool:
    snoozes = anti_skip_state.get("snoozes", {})
    entry = snoozes.get(lesson.exact_key)
    if not isinstance(entry, dict):
        return False
    try:
        due_at = datetime.fromisoformat(entry["due_at"])
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=core.TIMEZONE)
    except Exception:
        return False
    return due_at > now


def notify_lesson(
    title: str,
    body: str,
    lesson: core.Lesson,
    anti_skip_state: dict,
    now: datetime,
    urgent: bool = False,
) -> None:
    """
    Una lezione = un messaggio Telegram. Aggiunge i pulsanti interattivi.
    Gli alert 🚨 possono essere duplicati in chat privata tramite
    TELEGRAM_URGENT_CHAT_ID, se configurato.
    """
    print(f"{title} — notifica elaborata (dettagli omessi dal log).")

    acknowledged = acknowledgement_is_recent(
        lesson,
        anti_skip_state,
        now,
    )
    snoozed = snooze_is_active(lesson, anti_skip_state, now)
    keyboard = callback_keyboard(
        lesson,
        acknowledged=acknowledged,
        snoozed=snoozed,
    )

    targets = [telegram_main_chat_id()]

    for chat_id in [x for x in targets if x]:
        try:
            telegram_send_html(
                title,
                body,
                chat_id,
                reply_markup=keyboard,
            )
        except Exception as exc:
            print(
                "Telegram notification failed: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )

    # Mantiene compatibilità con l'eventuale canale WhatsApp già previsto.
    try:
        core.whatsapp_notify(title, body)
    except Exception as exc:
        print(
            "WhatsApp notification failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )


def notify_urgent_generic(
    title: str,
    body: str,
    anti_skip_state: dict,
) -> None:
    """
    Alert urgente senza una lezione identificabile: gruppo + eventuale chat
    privata urgente. Non usa pulsanti di conferma perché manca un exact_key.
    """
    print(f"{title} — notifica elaborata (dettagli omessi dal log).")

    targets = [telegram_main_chat_id()]

    for chat_id in [x for x in targets if x]:
        try:
            telegram_send_html(title, body, chat_id)
        except Exception as exc:
            print(
                "Telegram notification failed: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )

    try:
        core.whatsapp_notify(title, body)
    except Exception as exc:
        print(
            "WhatsApp notification failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )


def answer_callback(
    callback_query_id: str,
    text: str,
    alert: bool = False,
) -> None:
    try:
        telegram_api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:180],
                "show_alert": alert,
            },
        )
    except Exception as exc:
        print(
            "Risposta callback Telegram non riuscita: "
            f"{type(exc).__name__}"
        )


def refresh_callback_keyboard(
    callback_query: dict,
    lesson: core.Lesson,
    acknowledged: bool,
    snoozed: bool,
) -> None:
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return

    try:
        telegram_api(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": callback_keyboard(
                    lesson,
                    acknowledged=acknowledged,
                    snoozed=snoozed,
                ),
            },
        )
    except Exception as exc:
        print(
            "Aggiornamento pulsanti Telegram non riuscito: "
            f"{type(exc).__name__}"
        )


def private_command_name(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].casefold()


def process_private_telegram_command(
    message: dict,
    anti_skip_state: dict,
) -> None:
    """
    Modalità group-only: nessun comando privato può attivare una seconda
    destinazione Telegram.
    """
    return

def process_telegram_updates(
    lessons: list[core.Lesson],
    meta_by_key: dict[str, dict],
    anti_skip_state: dict,
    now: datetime,
) -> None:
    """
    Legge callback e comandi privati senza webhook. Il monitor gira già ogni
    5 minuti: i click vengono quindi registrati al run successivo.
    """
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return

    main_chat = telegram_main_chat_id()
    if not main_chat:
        return

    lesson_by_key = {lesson.exact_key: lesson for lesson in lessons}
    acknowledged = anti_skip_state.setdefault("acknowledged", {})
    snoozes = anti_skip_state.setdefault("snoozes", {})
    offset = int(anti_skip_state.get("telegram_update_offset", 0) or 0)

    for _ in range(5):
        try:
            response = telegram_api(
                "getUpdates",
                {
                    "offset": offset,
                    "limit": 100,
                    "timeout": 0,
                    "allowed_updates": ["callback_query", "message"],
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
            update_id = int(update.get("update_id", 0))
            offset = max(offset, update_id + 1)
            anti_skip_state["telegram_update_offset"] = offset

            message_update = update.get("message")
            if isinstance(message_update, dict):
                process_private_telegram_command(
                    message_update,
                    anti_skip_state,
                )
                continue

            query = update.get("callback_query")
            if not isinstance(query, dict):
                continue

            query_id = str(query.get("id", ""))
            data = str(query.get("data", ""))
            message = query.get("message") or {}
            chat = message.get("chat") or {}
            from_user = query.get("from") or {}

            chat_id = str(chat.get("id", ""))
            user_id = str(from_user.get("id", ""))

            allowed_chats = telegram_allowed_callback_chats(
                anti_skip_state,
            )
            if chat_id not in allowed_chats:
                answer_callback(
                    query_id,
                    "Questo pulsante non è autorizzato in questa chat.",
                    alert=True,
                )
                continue

            owner_id = telegram_owner_user_id(anti_skip_state)
            if owner_id and user_id != owner_id:
                answer_callback(
                    query_id,
                    "Questo pulsante è riservato al proprietario del monitor.",
                    alert=True,
                )
                continue

            match = re.fullmatch(
                rf"{re.escape(CALLBACK_PREFIX)}:(ack|snooze):([0-9a-f]{{16}})",
                data,
            )
            if not match:
                answer_callback(query_id, "Pulsante non riconosciuto.")
                continue

            action, lesson_key = match.groups()
            lesson = lesson_by_key.get(lesson_key)
            if lesson is None:
                answer_callback(
                    query_id,
                    "Questa programmazione non è più attuale.",
                    alert=True,
                )
                continue

            if now >= lesson.end_dt:
                answer_callback(
                    query_id,
                    "Questa didattica risulta già terminata.",
                    alert=True,
                )
                continue

            user_name = core.normalize_space(
                " ".join(
                    x
                    for x in [
                        str(from_user.get("first_name", "")),
                        str(from_user.get("last_name", "")),
                    ]
                    if x
                )
            )

            if action == "ack":
                acknowledged[lesson_key] = {
                    "at": now.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                }
                snoozes.pop(lesson_key, None)

                answer_callback(query_id, "✅ Preso nota.")
                refresh_callback_keyboard(
                    query,
                    lesson,
                    acknowledged=True,
                    snoozed=False,
                )

            elif action == "snooze":
                due_at = now + timedelta(minutes=SNOOZE_MINUTES)

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
                        fallback_meta(lesson),
                    ),
                    "user_id": user_id,
                    "user_name": user_name,
                }

                answer_callback(
                    query_id,
                    f"⏰ Ti ricordo questa didattica tra {SNOOZE_MINUTES} minuti.",
                )
                refresh_callback_keyboard(
                    query,
                    lesson,
                    acknowledged=True,
                    snoozed=True,
                )

        if len(updates) < 100:
            break


def clear_lesson_interactions(
    lesson: core.Lesson,
    anti_skip_state: dict,
) -> None:
    anti_skip_state.setdefault("acknowledged", {}).pop(
        lesson.exact_key,
        None,
    )
    anti_skip_state.setdefault("snoozes", {}).pop(
        lesson.exact_key,
        None,
    )


def process_due_snoozes(
    lessons: list[core.Lesson],
    meta_by_key: dict[str, dict],
    anti_skip_state: dict,
    reminded: set[str],
    now: datetime,
) -> None:
    lesson_by_key = {lesson.exact_key: lesson for lesson in lessons}
    snoozes = anti_skip_state.setdefault("snoozes", {})

    for lesson_key, entry in list(snoozes.items()):
        lesson = lesson_by_key.get(lesson_key)
        if lesson is None or now >= lesson.end_dt:
            snoozes.pop(lesson_key, None)
            continue

        try:
            due_at = datetime.fromisoformat(entry["due_at"])
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=core.TIMEZONE)
        except Exception:
            snoozes.pop(lesson_key, None)
            continue

        if now < due_at:
            continue

        minutes = (lesson.start_dt - now).total_seconds() / 60
        meta = meta_by_key.get(
            lesson_key,
            entry.get("meta") or fallback_meta(lesson),
        )

        if lesson.start_dt <= now < lesson.end_dt:
            title = "🚨 PROMEMORIA SU RICHIESTA · DIDATTICA INIZIATA"
            body = (
                lesson_card(lesson, meta)
                + "\n\n⚠️ La didattica risulta già iniziata."
            )
            urgent = True
        else:
            title = f"⏰ PROMEMORIA SU RICHIESTA · {SNOOZE_MINUTES} MIN"
            body = reminder_card(lesson, minutes, meta)
            urgent = minutes <= 5

        notify_lesson(
            title,
            body,
            lesson,
            anti_skip_state,
            now,
            urgent=urgent,
        )

        # Il promemoria richiesto sostituisce eventuali reminder standard
        # già maturati nello stesso momento. I successivi (es. 5 min) restano.
        mark_current_notice_as_covered(lesson, now, reminded)
        snoozes.pop(lesson_key, None)


def cleanup_interaction_state(
    lessons: list[core.Lesson],
    anti_skip_state: dict,
) -> None:
    active = {lesson.exact_key for lesson in lessons}

    acknowledged = anti_skip_state.setdefault("acknowledged", {})
    for key in list(acknowledged):
        if key not in active:
            acknowledged.pop(key, None)

    snoozes = anti_skip_state.setdefault("snoozes", {})
    for key in list(snoozes):
        if key not in active:
            snoozes.pop(key, None)


def normalize_identity(value: str) -> str:
    value = core.normalize_space(value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return core.normalize_space(value)


def reminder_state_key(lesson: core.Lesson, threshold: int) -> str:
    value = f"{lesson.exact_key}|telegram-reminder|{threshold}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def anti_skip_state_key(lesson: core.Lesson, kind: str) -> str:
    value = f"{lesson.exact_key}|anti-skip|{kind}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def human_minutes(minutes: float) -> str:
    total = max(0, int(round(minutes)))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{max(1, mins)} min"


def threshold_title(threshold: int) -> str:
    if threshold == 120:
        return "⏰ DIDATTICA IN ARRIVO · 2 ORE"
    if threshold == 15:
        return "⚠️ DIDATTICA IN ARRIVO · 15 MIN"
    if threshold == 5:
        return "🚨 DIDATTICA IN ARRIVO · 5 MIN"
    return f"⏰ DIDATTICA IN ARRIVO · {threshold} MIN"


def unique_lessons(items: list[core.Lesson]) -> list[core.Lesson]:
    output: dict[str, core.Lesson] = {}
    for lesson in items:
        output[lesson.exact_key] = lesson
    return sorted(
        output.values(),
        key=lambda x: (x.start_dt, x.end_dt, x.description.casefold()),
    )


def lesson_meta_for(
    lesson: core.Lesson,
    new_meta: dict[str, dict],
    old_meta: dict[str, dict],
) -> dict:
    return (
        new_meta.get(lesson.exact_key)
        or old_meta.get(lesson.exact_key)
        or fallback_meta(lesson)
    )


def match_live_identity_lesson(
    lessons: list[core.Lesson],
    new_meta: dict[str, dict],
    old_meta: dict[str, dict],
    live_evidence: dict,
) -> core.Lesson | None:
    """
    Prova a riconoscere quale lezione è mostrata nella scheda In corso.

    Priorità:
    1. stesso ID Mercatorum presente in un link visibile;
    2. materia/titolo presenti nel testo visibile.

    In caso di ambiguità non indovina: restituisce None.
    """
    if not isinstance(live_evidence, dict):
        return None

    live_text = normalize_identity(
        str(live_evidence.get("text", ""))
    )

    live_ids: set[str] = set()

    for href in live_evidence.get("hrefs", []) or []:
        match = re.search(
            r"/class/(?:on-demand|test)/([^/?#]+)",
            str(href),
            flags=re.IGNORECASE,
        )
        if match:
            live_ids.add(
                core.normalize_space(match.group(1))
            )

    scored: list[tuple[int, core.Lesson]] = []

    for lesson in lessons:
        meta = lesson_meta_for(
            lesson,
            new_meta,
            old_meta,
        )

        mercatorum_id = core.normalize_space(
            meta.get("mercatorum_id", "")
        )

        if mercatorum_id and mercatorum_id in live_ids:
            scored.append((100, lesson))
            continue

        subject = normalize_identity(
            meta.get("subject", "")
        )
        title = normalize_identity(
            meta.get("title", "")
        )
        description = normalize_identity(
            lesson.description
        )

        score = 0

        if title and len(title) >= 8 and title in live_text:
            score += 6

        if subject and len(subject) >= 5 and subject in live_text:
            score += 3

        if (
            description
            and len(description) >= 12
            and description in live_text
        ):
            score += 8

        if score >= 6:
            scored.append((score, lesson))

    if not scored:
        return None

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score = scored[0][0]
    best = [
        lesson
        for score, lesson in scored
        if score == best_score
    ]

    if len(best) != 1:
        return None

    return best[0]

def mark_current_notice_as_covered(
    lesson: core.Lesson,
    now: datetime,
    reminded: set[str],
) -> None:
    minutes = (lesson.start_dt - now).total_seconds() / 60

    for threshold in TELEGRAM_REMINDER_MINUTES:
        if minutes <= threshold:
            reminded.add(reminder_state_key(lesson, threshold))

    if lesson.start_dt.date() == now.date() and now.hour >= SAME_DAY_HOUR:
        reminded.add(anti_skip_state_key(lesson, "same-day"))

    tomorrow = now.date() + timedelta(days=1)
    if lesson.start_dt.date() == tomorrow and now.hour >= DAY_BEFORE_HOUR:
        reminded.add(anti_skip_state_key(lesson, "day-before"))


def notify_added_anti_skip(
    lesson: core.Lesson,
    meta: dict,
    now: datetime,
    reminded: set[str],
    anti_skip_state: dict,
) -> None:
    minutes = (lesson.start_dt - now).total_seconds() / 60
    body = lesson_card(lesson, meta)

    if lesson.start_dt <= now < lesson.end_dt:
        title = "🚨 DIDATTICA RILEVATA GIÀ IN CORSO"
        body += (
            "\n\n⚠️ L'orario di inizio è già trascorso. "
            "Controlla subito Mercatorum."
        )
        mark_current_notice_as_covered(lesson, now, reminded)
    elif minutes <= 45:
        title = "🚨 DIDATTICA AGGIUNTA · MENO DI 45 MIN"
        body += f"\n\n⚠️ Mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(lesson, now, reminded)
    elif minutes <= 180:
        title = "🚨 DIDATTICA AGGIUNTA ALL'ULTIMO MOMENTO"
        body += f"\n\n⚠️ Mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(lesson, now, reminded)
    elif minutes <= 1440:
        title = "⚠️ DIDATTICA AGGIUNTA CON POCO PREAVVISO"
        body += f"\n\nMancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(lesson, now, reminded)
    else:
        title = "🆕 DIDATTICA AGGIUNTA"

    notify_lesson(
        title,
        body,
        lesson,
        anti_skip_state,
        now,
        urgent=title.startswith("🚨"),
    )


def notify_modified_anti_skip(
    before: core.Lesson,
    after: core.Lesson,
    before_meta: dict,
    after_meta: dict,
    now: datetime,
    reminded: set[str],
    anti_skip_state: dict,
) -> None:
    message = modified_card(before, after, before_meta, after_meta)
    normal_title, _, body = message.partition("\n")
    body = body.lstrip()

    moved_earlier = after.start_dt < before.start_dt
    minutes = (after.start_dt - now).total_seconds() / 60

    if moved_earlier and after.start_dt <= now < after.end_dt:
        title = "🚨 DIDATTICA ANTICIPATA ED È GIÀ INIZIATA"
        body += (
            "\n\n⚠️ La nuova programmazione risulta già iniziata. "
            "Controlla subito Mercatorum."
        )
        mark_current_notice_as_covered(after, now, reminded)
    elif moved_earlier and minutes <= 45:
        title = "🚨 DIDATTICA ANTICIPATA · MENO DI 45 MIN"
        body += f"\n\n⚠️ Alla nuova ora mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(after, now, reminded)
    elif moved_earlier and minutes <= 180:
        title = "🚨 DIDATTICA ANTICIPATA ALL'ULTIMO MOMENTO"
        body += f"\n\n⚠️ Alla nuova ora mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(after, now, reminded)
    elif moved_earlier and minutes <= 1440:
        title = "⚠️ DIDATTICA ANTICIPATA CON POCO PREAVVISO"
        body += f"\n\nAlla nuova ora mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(after, now, reminded)
    else:
        title = normal_title

    notify_lesson(
        title,
        body,
        after,
        anti_skip_state,
        now,
        urgent=title.startswith("🚨"),
    )


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
    message = recovery_card(
        old_lesson,
        new_lesson,
        old_meta,
        new_meta,
        certain,
    )
    title, _, body = message.partition("\n")
    body = body.lstrip()

    minutes = (new_lesson.start_dt - now).total_seconds() / 60
    if new_lesson.start_dt <= now < new_lesson.end_dt:
        title = "🚨 RECUPERO / RIPROGRAMMAZIONE GIÀ IN CORSO"
        body += "\n\n⚠️ Controlla subito Mercatorum."
        mark_current_notice_as_covered(new_lesson, now, reminded)
    elif minutes <= 180:
        title = "🚨 " + title.lstrip("♻️ ").strip()
        body += f"\n\n⚠️ Mancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(new_lesson, now, reminded)
    elif minutes <= 1440:
        title = "⚠️ " + title.lstrip("♻️ ").strip()
        body += f"\n\nMancano circa **{human_minutes(minutes)}**."
        mark_current_notice_as_covered(new_lesson, now, reminded)

    notify_lesson(
        title,
        body,
        new_lesson,
        anti_skip_state,
        now,
        urgent=title.startswith("🚨"),
    )


def ensure_calendar_safety_reminders(
    lessons: list[core.Lesson],
    calendar_events: dict[str, str],
    anti_skip_state: dict,
) -> None:
    if anti_skip_state.get("calendar_safety_v1"):
        return

    service, calendar_id = core.google_calendar_service()
    if service is None or not calendar_events:
        return

    complete = True
    for lesson in lessons:
        event_id = calendar_events.get(lesson.exact_key)
        if not event_id:
            continue
        try:
            core.calendar_update(service, calendar_id, event_id, lesson)
        except Exception as exc:
            print(
                "Aggiornamento reminder Calendar non riuscito: "
                f"{type(exc).__name__}"
            )
            complete = False

    if complete:
        anti_skip_state["calendar_safety_v1"] = True


def fmt_date(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%d/%m/%Y")


def lesson_name(lesson: core.Lesson) -> str:
    return core.normalize_space(lesson.description) or "Didattica sincrona"


def fallback_meta(lesson: core.Lesson) -> dict:
    return {
        "subject": "",
        "title": lesson_name(lesson),
        "mercatorum_id": "",
    }


def lesson_card(lesson: core.Lesson, meta: dict | None = None) -> str:
    meta = meta or {}
    subject = core.normalize_space(meta.get("subject", ""))
    title = core.normalize_space(meta.get("title", ""))

    lines: list[str] = []
    if subject:
        lines.append(f"**{subject}**")
    if title:
        lines.append(f"**{title}**")
    elif not subject:
        lines.append(f"**{lesson_name(lesson)}**")

    lines += [
        "",
        f"📅 {fmt_date(lesson.date)}",
        f"🕒 {lesson.start}–{lesson.end}",
    ]
    return "\n".join(lines)


def modified_card(
    before: core.Lesson,
    after: core.Lesson,
    before_meta: dict | None,
    after_meta: dict | None,
) -> str:
    before_meta = before_meta or fallback_meta(before)
    after_meta = after_meta or fallback_meta(after)

    date_changed = before.date != after.date
    time_changed = (before.start, before.end) != (after.start, after.end)

    if date_changed and time_changed:
        header = "📅🕒 GIORNO E ORARIO MODIFICATI"
    elif date_changed:
        header = "📅 GIORNO MODIFICATO"
    elif time_changed:
        header = "🕒 ORARIO MODIFICATO"
    else:
        header = "✏️ DIDATTICA MODIFICATA"

    old_subject = core.normalize_space(before_meta.get("subject", ""))
    old_title = core.normalize_space(before_meta.get("title", ""))
    new_subject = core.normalize_space(after_meta.get("subject", ""))
    new_title = core.normalize_space(after_meta.get("title", ""))

    lines = [header, ""]

    if new_subject:
        lines.append(f"**{new_subject}**")
    if new_title:
        lines.append(f"**{new_title}**")
    if not new_subject and not new_title:
        lines.append(f"**{lesson_name(after)}**")

    if date_changed or time_changed:
        lines += [
            "",
            "**PRIMA**",
            f"📅 {fmt_date(before.date)}",
            f"🕒 {before.start}–{before.end}",
            "",
            "**ORA**",
            f"📅 {fmt_date(after.date)}",
            f"🕒 {after.start}–{after.end}",
        ]

    old_label = " · ".join(x for x in [old_subject, old_title] if x)
    new_label = " · ".join(x for x in [new_subject, new_title] if x)

    if old_label and new_label and old_label != new_label:
        lines += [
            "",
            "**DIDATTICA PRIMA**",
            old_label,
            "",
            "**DIDATTICA ORA**",
            new_label,
        ]

    return "\n".join(lines)


def reminder_card(
    lesson: core.Lesson,
    actual_minutes: float,
    meta: dict | None,
) -> str:
    return (
        f"{lesson_card(lesson, meta)}\n"
        f"⏳ Inizia tra circa {max(1, round(actual_minutes))} min"
    )


def recovery_card(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
) -> str:
    header = (
        "♻️ DIDATTICA RECUPERATA"
        if certain
        else "♻️ POSSIBILE RECUPERO / RIPROGRAMMAZIONE"
    )

    subject = core.normalize_space(
        new_meta.get("subject", "") or old_meta.get("subject", "")
    )
    title = core.normalize_space(
        new_meta.get("title", "") or old_meta.get("title", "")
    )

    lines = [header, ""]
    if subject:
        lines.append(f"**{subject}**")
    if title:
        lines.append(f"**{title}**")
    if not subject and not title:
        lines.append(f"**{lesson_name(new_lesson)}**")

    lines += [
        "",
        "**VECCHIA PROGRAMMAZIONE**",
        f"📅 {fmt_date(old_lesson.date)}",
        f"🕒 {old_lesson.start}–{old_lesson.end}",
        "",
        "**NUOVA PROGRAMMAZIONE**",
        f"📅 {fmt_date(new_lesson.date)}",
        f"🕒 {new_lesson.start}–{new_lesson.end}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Estrazione robusta delle righe Programmate
# ---------------------------------------------------------------------------

def _row_date(tokens: list[str]) -> str | None:
    day = month = year = None

    for token in tokens[:12]:
        clean = core.normalize_space(token)
        low = clean.casefold()

        if day is None and clean.isdigit():
            number = int(clean)
            if 1 <= number <= 31:
                day = number
                continue

        if month is None and low in core.MONTHS:
            month = core.MONTHS[low]
            continue

        if year is None and YEAR_RE.fullmatch(clean):
            year = int(clean)

    if day and month and year:
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _row_times(tokens: list[str]) -> tuple[str | None, str | None, int | None]:
    start = end = None
    end_index = None

    for index, token in enumerate(tokens):
        low = core.normalize_space(token).casefold()

        if low == "inizio":
            for j in range(index + 1, min(index + 4, len(tokens))):
                candidate = core.normalize_space(tokens[j])
                if TIME_RE.fullmatch(candidate):
                    start = candidate
                    break

        if low == "fine":
            for j in range(index + 1, min(index + 4, len(tokens))):
                candidate = core.normalize_space(tokens[j])
                if TIME_RE.fullmatch(candidate):
                    end = candidate
                    end_index = j
                    break

    return start, end, end_index


def _content_after_end(tokens: list[str], end_index: int) -> list[str]:
    ignored = {
        "inizio",
        "fine",
        "test",
        "% presenza",
        "registrazione",
        "accedi al test",
        "giustificativo",
    }
    result: list[str] = []

    for token in tokens[end_index + 1:]:
        clean = core.normalize_space(token)
        low = clean.casefold()

        # Se il contenitore DOM selezionato include il footer/banner cookie,
        # fermiamoci prima che quei testi finiscano nel titolo della lezione.
        if has_footer_noise(clean):
            break

        if not clean or low in ignored:
            continue
        if TIME_RE.fullmatch(clean) or YEAR_RE.fullmatch(clean):
            continue
        if WEEKDAY_RE.fullmatch(clean) or low in core.MONTHS:
            continue
        if clean.isdigit() and 0 <= int(clean) <= 31:
            continue
        if re.fullmatch(r"\d+%", clean) or clean == "-":
            continue

        result.append(clean)

    return result


def extract_visible_rows(page) -> list[dict]:
    raw_rows = page.evaluate(
        """
        () => {
          const leaves = Array.from(document.querySelectorAll('body *'))
            .filter(el =>
              el.children.length === 0 &&
              (el.textContent || '').trim().toLowerCase() === 'inizio'
            );

          const output = [];
          const seen = new Set();

          for (const leaf of leaves) {
            let node = leaf;
            let row = null;

            for (let depth = 0; depth < 10 && node.parentElement; depth++) {
              node = node.parentElement;
              const text = (node.innerText || '').trim();
              const times = text.match(/\\b\\d{1,2}:\\d{2}\\b/g) || [];

              if (
                /\\bFine\\b/i.test(text) &&
                /\\b20\\d{2}\\b/.test(text) &&
                times.length >= 2 &&
                text.length >= 40 &&
                text.length <= 1500
              ) {
                row = node;
                break;
              }
            }

            if (!row) continue;

            const text = (row.innerText || '').trim();
            const key = text.replace(/\\s+/g, ' ');
            if (seen.has(key)) continue;
            seen.add(key);

            const tokens = Array.from(row.querySelectorAll('*'))
              .filter(el => el.children.length === 0)
              .map(el => (el.innerText || el.textContent || '').trim())
              .filter(Boolean);

            const hrefs = Array.from(row.querySelectorAll('a[href]'))
              .map(a => a.href)
              .filter(Boolean);

            output.push({tokens, hrefs});
          }

          return output;
        }
        """
    )

    parsed: list[dict] = []

    for raw in raw_rows:
        tokens = [
            core.normalize_space(x)
            for x in raw.get("tokens", [])
            if core.normalize_space(x)
        ]

        date_iso = _row_date(tokens)
        start, end, end_index = _row_times(tokens)
        if not date_iso or not start or not end or end_index is None:
            continue

        content = _content_after_end(tokens, end_index)
        if len(content) < 2:
            continue

        subject = content[0]
        title = core.normalize_space(" ".join(content[1:]))
        description = core.normalize_space(f"{subject} {title}")

        mercatorum_id = ""
        for href in raw.get("hrefs", []):
            match = re.search(
                r"/class/(?:on-demand|test)/([^/?#]+)",
                href,
                flags=re.IGNORECASE,
            )
            if match:
                mercatorum_id = match.group(1)
                break

        lesson = core.Lesson(
            date=date_iso,
            start=start,
            end=end,
            description=description or "Didattica sincrona",
        )

        parsed.append(
            {
                "lesson": lesson,
                "meta": {
                    "subject": subject,
                    "title": title,
                    "mercatorum_id": mercatorum_id,
                },
            }
        )

    # Deduplica semantica: stesso giorno/orario/materia/titolo = una sola riga.
    deduped: dict[tuple[str, str, str, str, str], dict] = {}
    for item in parsed:
        lesson = item["lesson"]
        meta = item["meta"]
        key = (
            lesson.date,
            lesson.start,
            lesson.end,
            normalize_identity(meta.get("subject", "")),
            normalize_identity(meta.get("title", "")),
        )
        if key not in deduped:
            deduped[key] = item

    return list(deduped.values())


def fallback_visible_rows(page) -> list[dict]:
    body = page.locator("body").inner_text(timeout=15_000)
    return [
        {"lesson": lesson, "meta": fallback_meta(lesson)}
        for lesson in core.parse_lessons(body)
    ]


def _scroll_metrics(page) -> dict:
    return page.evaluate(
        """
        () => {
          const scrolling = document.scrollingElement || document.documentElement;
          return {
            top: Math.round(scrolling.scrollTop),
            height: Math.round(scrolling.scrollHeight),
            client: Math.round(scrolling.clientHeight)
          };
        }
        """
    )


def _scroll_bottom(page) -> None:
    page.evaluate(
        """
        () => {
          const scrolling = document.scrollingElement || document.documentElement;
          scrolling.scrollTop = scrolling.scrollHeight;
          window.scrollTo(0, scrolling.scrollHeight);

          const candidates = Array.from(document.querySelectorAll('*'))
            .filter(el => {
              const style = getComputedStyle(el);
              return (
                /(auto|scroll)/.test(style.overflowY) &&
                el.scrollHeight > el.clientHeight + 100
              );
            })
            .sort(
              (a, b) =>
                (b.scrollHeight - b.clientHeight) -
                (a.scrollHeight - a.clientHeight)
            );

          if (candidates.length) {
            candidates[0].scrollTop = candidates[0].scrollHeight;
          }
        }
        """
    )


def load_all_programmed(page) -> tuple[list[core.Lesson], dict[str, dict]]:
    """
    Mercatorum carica altre Programmate quando si arriva in fondo.
    Continuiamo finché numero di lezioni + altezza pagina restano invariati
    per 3 cicli consecutivi. Accumuliamo le righe durante lo scroll per
    resistere anche a eventuale virtualizzazione del DOM.
    """
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    collected: dict[str, core.Lesson] = {}
    meta_by_key: dict[str, dict] = {}
    stable_cycles = 0
    previous_signature: tuple[int, int] | None = None

    for _ in range(60):
        rows = extract_visible_rows(page) or fallback_visible_rows(page)

        for item in rows:
            lesson = item["lesson"]
            collected[lesson.exact_key] = lesson
            meta_by_key[lesson.exact_key] = item["meta"]

        _scroll_bottom(page)
        page.wait_for_timeout(2000)

        rows = extract_visible_rows(page) or fallback_visible_rows(page)
        for item in rows:
            lesson = item["lesson"]
            collected[lesson.exact_key] = lesson
            meta_by_key[lesson.exact_key] = item["meta"]

        metrics = _scroll_metrics(page)
        signature = (len(collected), metrics["height"])

        if signature == previous_signature:
            stable_cycles += 1
        else:
            stable_cycles = 0

        previous_signature = signature

        if stable_cycles >= 3:
            break
    else:
        raise RuntimeError(
            "Il caricamento progressivo delle Programmate non si è stabilizzato."
        )

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    if not collected:
        raise RuntimeError(
            "Nessuna didattica programmata riconosciuta. "
            "Non aggiorno lo stato per evitare false cancellazioni."
        )

    lessons = sorted(
        collected.values(),
        key=lambda x: (x.start_dt, x.end_dt, x.description.casefold()),
    )
    return lessons, meta_by_key


# ---------------------------------------------------------------------------
# Scheda In corso
# ---------------------------------------------------------------------------

def click_live_tab(page) -> None:
    tab = core.first_visible(
        page,
        [
            "[role='tab']:has-text('In corso')",
            "button:has-text('In corso')",
            "a:has-text('In corso')",
            "text=In corso",
        ],
    )
    if not tab:
        raise RuntimeError("Non trovo la scheda 'In corso'.")

    tab.click(timeout=10_000)
    core.settle_spa(page, 1500)


def page_live_status(page) -> bool:
    text = core.normalize_space(
        page.locator("body").inner_text(timeout=15_000)
    ).casefold()

    if "didattica sincrona" not in text:
        raise RuntimeError(
            "La pagina 'In corso' non sembra caricata correttamente."
        )

    return EMPTY_LIVE_TEXT not in text


def live_page_evidence(page) -> dict:
    """
    Raccoglie informazioni visibili dalla scheda In corso senza scriverle
    nei log. Servono solo per riconoscere materia/titolo della diretta.
    """
    text = core.normalize_space(
        page.locator("body").inner_text(timeout=15_000)
    )

    try:
        hrefs = page.locator("a[href]:visible").evaluate_all(
            """
            elements => elements
              .map(element => element.href || '')
              .filter(Boolean)
            """
        )
    except Exception:
        hrefs = []

    return {
        "text": text,
        "hrefs": hrefs,
    }


def detect_live_present(page) -> tuple[bool, dict]:
    click_live_tab(page)

    first = page_live_status(page)
    if not first:
        return False, {}

    page.wait_for_timeout(3000)

    second = page_live_status(page)
    if not second:
        return False, {}

    return True, live_page_evidence(page)

# ---------------------------------------------------------------------------
# Login + snapshot
# ---------------------------------------------------------------------------

def scrape_snapshot() -> tuple[list[core.Lesson], dict[str, dict], bool | None, dict]:
    core.DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
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
                ["#password", "input[type='password']"],
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
                ["#password", "input[type='password']"],
            ):
                raise RuntimeError(
                    "Mercatorum mostra ancora la pagina di login."
                )

            lessons, meta_by_key = load_all_programmed(page)

            try:
                live_present, live_evidence = detect_live_present(page)
            except Exception as exc:
                print(
                    "Controllo 'In corso' non riuscito: "
                    f"{type(exc).__name__}"
                )
                live_present = None
                live_evidence = {}

            core.best_effort_logout(page)
            return lessons, meta_by_key, live_present, live_evidence

        except Exception:
            try:
                page.screenshot(
                    path=str(core.DEBUG_DIR / "failure.png"),
                    full_page=True,
                )
            except Exception:
                pass
            raise

        finally:
            context.close()
            browser.close()


def scrape_snapshot_with_retry(
    attempts: int = SNAPSHOT_ATTEMPTS,
    wait_seconds: int = SNAPSHOT_RETRY_WAIT_SECONDS,
) -> tuple[list[core.Lesson], dict[str, dict], bool | None, dict]:
    """
    Retry completo contro errori temporanei di login/rendering Mercatorum.
    Ogni tentativo usa una nuova sessione browser tramite scrape_snapshot().
    """
    attempts = max(1, attempts)

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                print(
                    "Nuovo tentativo snapshot Mercatorum "
                    f"{attempt}/{attempts}..."
                )

            result = scrape_snapshot()

            if attempt > 1:
                print(
                    "Snapshot Mercatorum recuperato con successo "
                    f"al tentativo {attempt}/{attempts}."
                )

            return result

        except Exception as exc:
            print(
                "Snapshot Mercatorum fallito "
                f"({attempt}/{attempts}): {type(exc).__name__}",
                file=sys.stderr,
            )

            if attempt >= attempts:
                raise

            if wait_seconds > 0:
                print(
                    f"Riprovo tra {wait_seconds} secondi "
                    "con una nuova sessione browser..."
                )
                time.sleep(wait_seconds)

    raise RuntimeError(
        "Retry snapshot Mercatorum terminato in modo inatteso."
    )


# ---------------------------------------------------------------------------
# Rimozioni, transizioni e recuperi
# ---------------------------------------------------------------------------

def filter_live_transitions(
    removed: list[core.Lesson],
    now: datetime,
    live_present: bool | None,
) -> tuple[list[core.Lesson], list[core.Lesson]]:
    candidates: list[core.Lesson] = []
    transitions: list[core.Lesson] = []

    for lesson in removed:
        if lesson.start_dt <= now:
            transitions.append(lesson)
            continue

        minutes = (lesson.start_dt - now).total_seconds() / 60

        if (
            minutes <= LIVE_TRANSITION_GRACE_MINUTES
            and live_present in {True, None}
        ):
            transitions.append(lesson)
        else:
            candidates.append(lesson)

    return candidates, transitions


def recovery_score(
    new_lesson: core.Lesson,
    new_meta: dict,
    item: dict,
) -> tuple[float, bool]:
    old_lesson = core.lesson_from_dict(item["lesson"])
    old_meta = item.get("meta", fallback_meta(old_lesson))

    new_id = core.normalize_space(new_meta.get("mercatorum_id", ""))
    old_id = core.normalize_space(old_meta.get("mercatorum_id", ""))

    if new_id and old_id and new_id == old_id:
        return 2.0, True

    new_subject = normalize_identity(new_meta.get("subject", ""))
    old_subject = normalize_identity(old_meta.get("subject", ""))
    new_title = normalize_identity(new_meta.get("title", ""))
    old_title = normalize_identity(old_meta.get("title", ""))

    subject_score = (
        SequenceMatcher(None, new_subject, old_subject).ratio()
        if new_subject and old_subject else 0.0
    )
    title_score = (
        SequenceMatcher(None, new_title, old_title).ratio()
        if new_title and old_title else 0.0
    )

    if subject_score >= 0.98 and title_score >= 0.98:
        return 1.5, False
    if subject_score >= 0.95 and title_score >= 0.90:
        return 1.2, False

    description_score = SequenceMatcher(
        None,
        normalize_identity(new_lesson.description),
        normalize_identity(old_lesson.description),
    ).ratio()

    return (1.0, False) if description_score >= 0.95 else (0.0, False)


def find_recovery(
    lesson: core.Lesson,
    meta: dict,
    history: list[dict],
) -> tuple[int | None, bool]:
    best_index = None
    best_score = 0.0
    best_certain = False

    # Dal più recente al più vecchio: a parità di punteggio vince il più recente.
    for index in range(len(history) - 1, -1, -1):
        score, certain = recovery_score(
            lesson,
            meta,
            history[index],
        )
        if score > best_score:
            best_index = index
            best_score = score
            best_certain = certain

    return (
        (best_index, best_certain)
        if best_score >= 1.0
        else (None, False)
    )


# ---------------------------------------------------------------------------
# Stato cifrato esteso
# ---------------------------------------------------------------------------

def save_extended_state(
    lessons: list[core.Lesson],
    reminded: set[str],
    calendar_events: dict[str, str],
    pending_missing: dict[str, dict],
    removed_history: list[dict],
    lesson_meta: dict[str, dict],
    anti_skip_state: dict,
) -> None:
    functional = {
        "events": [asdict(x) for x in lessons],
        "reminded": sorted(reminded),
        "calendar_events": calendar_events,
        "pending_missing": pending_missing,
        "removed_history": removed_history,
        "lesson_meta": lesson_meta,
        "anti_skip": anti_skip_state,
    }

    existing = core.load_state() if core.STATE_PATH.exists() else None

    if existing is not None:
        current = {
            "events": existing.get("events", []),
            "reminded": sorted(existing.get("reminded", [])),
            "calendar_events": existing.get("calendar_events", {}),
            "pending_missing": existing.get("pending_missing", {}),
            "removed_history": existing.get("removed_history", []),
            "lesson_meta": existing.get("lesson_meta", {}),
            "anti_skip": existing.get("anti_skip", {}),
        }
        if current == functional:
            return

    payload = {
        "updated_at": datetime.now(core.TIMEZONE).isoformat(),
        **functional,
    }
    plaintext = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    encrypted = core.state_fernet().encrypt(plaintext)
    temp = core.STATE_PATH.with_suffix(core.STATE_PATH.suffix + ".tmp")
    temp.write_bytes(encrypted)
    temp.replace(core.STATE_PATH)

    if (
        core.LEGACY_STATE_PATH.exists()
        and core.LEGACY_STATE_PATH.resolve() != core.STATE_PATH.resolve()
    ):
        core.LEGACY_STATE_PATH.unlink()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = core.load_state()

    old_lessons = [
        core.lesson_from_dict(x)
        for x in state.get("events", [])
    ]
    reminded = set(state.get("reminded", []))
    calendar_events = dict(state.get("calendar_events", {}))
    pending_missing = dict(state.get("pending_missing", {}))
    removed_history = list(state.get("removed_history", []))
    old_meta = dict(state.get("lesson_meta", {}))
    anti_skip_state = dict(state.get("anti_skip", {}))

    # Telegram group-only: elimina definitivamente eventuali configurazioni
    # private salvate dalle vecchie versioni Anti-Salto.
    anti_skip_state.pop("telegram_owner_user_id", None)
    anti_skip_state.pop("telegram_urgent_chat_id", None)

    silent_calendar_removed: list[core.Lesson] = []
    clean_old_lessons: list[core.Lesson] = []
    for lesson in old_lessons:
        meta = old_meta.get(lesson.exact_key, fallback_meta(lesson))
        if is_dirty_lesson(lesson, meta):
            silent_calendar_removed.append(lesson)
            pending_missing.pop(lesson.exact_key, None)
            old_meta.pop(lesson.exact_key, None)
        else:
            clean_old_lessons.append(lesson)
    old_lessons = clean_old_lessons

    clean_history: list[dict] = []
    for item in removed_history:
        try:
            history_lesson = core.lesson_from_dict(item["lesson"])
            history_meta = item.get("meta", fallback_meta(history_lesson))
        except Exception:
            continue
        if not is_dirty_lesson(history_lesson, history_meta):
            clean_history.append(item)
    removed_history = clean_history

    new_lessons, new_meta, live_present, live_evidence = scrape_snapshot_with_retry()

    clean_new_lessons: list[core.Lesson] = []
    clean_new_meta: dict[str, dict] = {}
    for lesson in new_lessons:
        meta = new_meta.get(lesson.exact_key, fallback_meta(lesson))
        if is_dirty_lesson(lesson, meta):
            continue
        clean_new_lessons.append(lesson)
        clean_new_meta[lesson.exact_key] = meta
    new_lessons = clean_new_lessons
    new_meta = clean_new_meta
    now = datetime.now(core.TIMEZONE)

    # Anti-Salto v2: raccoglie i click ai pulsanti solo dopo uno snapshot
    # riuscito, così un errore di scraping non rischia di perdere interazioni.
    process_telegram_updates(
        new_lessons,
        new_meta,
        anti_skip_state,
        now,
    )

    cutoff = now - timedelta(days=REMOVED_HISTORY_DAYS)
    history: list[dict] = []
    for item in removed_history:
        try:
            removed_at = datetime.fromisoformat(item["removed_at"])
            if removed_at.tzinfo is None:
                removed_at = removed_at.replace(tzinfo=core.TIMEZONE)
        except Exception:
            continue
        if removed_at >= cutoff:
            history.append(item)
    removed_history = history

    added: list[core.Lesson] = []
    modified: list[tuple[core.Lesson, core.Lesson]] = []
    confirmed_removed: list[core.Lesson] = []
    pending_preserved: list[core.Lesson] = []
    transitions: list[core.Lesson] = []

    if not old_lessons:
        core.notify(
            "✅ Mercatorum monitor attivato",
            (
                "Monitor attivato. "
                f"Ho trovato {len(new_lessons)} didattiche sincrone programmate."
            ),
        )
    else:
        old_relevant = [x for x in old_lessons if x.end_dt > now]
        new_relevant = [x for x in new_lessons if x.end_dt > now]

        added, raw_removed, modified = core.diff_lessons(
            old_relevant,
            new_relevant,
        )
        candidates, transitions = filter_live_transitions(
            raw_removed,
            now,
            live_present,
        )

        current_keys = {x.exact_key for x in new_lessons}

        for key in list(pending_missing):
            if key in current_keys:
                pending_missing.pop(key, None)

        for before, _ in modified:
            pending_missing.pop(before.exact_key, None)

        candidate_keys = {x.exact_key for x in candidates}

        for lesson in candidates:
            key = lesson.exact_key
            entry = pending_missing.get(
                key,
                {
                    "lesson": asdict(lesson),
                    "meta": old_meta.get(key, fallback_meta(lesson)),
                    "count": 0,
                    "first_seen": now.isoformat(),
                },
            )
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_seen"] = now.isoformat()

            if entry["count"] >= MISSING_CONFIRM_CHECKS:
                confirmed_removed.append(lesson)
                removed_history.append(
                    {
                        "lesson": asdict(lesson),
                        "meta": entry.get(
                            "meta",
                            old_meta.get(key, fallback_meta(lesson)),
                        ),
                        "removed_at": now.isoformat(),
                    }
                )
                pending_missing.pop(key, None)
            else:
                pending_missing[key] = entry
                pending_preserved.append(lesson)

        for key in list(pending_missing):
            if key in candidate_keys:
                continue

            entry = pending_missing[key]
            try:
                lesson = core.lesson_from_dict(entry["lesson"])
            except Exception:
                pending_missing.pop(key, None)
                continue

            if lesson.exact_key in current_keys:
                pending_missing.pop(key, None)
                continue

            if lesson.start_dt <= now:
                pending_missing.pop(key, None)
                transitions.append(lesson)
                continue

            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_seen"] = now.isoformat()

            if entry["count"] >= MISSING_CONFIRM_CHECKS:
                confirmed_removed.append(lesson)
                removed_history.append(
                    {
                        "lesson": asdict(lesson),
                        "meta": entry.get(
                            "meta",
                            old_meta.get(key, fallback_meta(lesson)),
                        ),
                        "removed_at": now.isoformat(),
                    }
                )
                pending_missing.pop(key, None)
            else:
                pending_missing[key] = entry
                pending_preserved.append(lesson)

    recoveries: list[
        tuple[core.Lesson, core.Lesson, dict, dict, bool]
    ] = []
    normal_added: list[core.Lesson] = []

    for lesson in added:
        meta = new_meta.get(lesson.exact_key, fallback_meta(lesson))
        match_index, certain = find_recovery(
            lesson,
            meta,
            removed_history,
        )

        if match_index is None:
            normal_added.append(lesson)
            continue

        item = removed_history.pop(match_index)
        old_lesson = core.lesson_from_dict(item["lesson"])
        old_lesson_meta = item.get("meta", fallback_meta(old_lesson))

        recoveries.append(
            (old_lesson, lesson, old_lesson_meta, meta, certain)
        )

    added = normal_added

    # Cambiamenti con priorità Anti-Salto.
    for lesson in added:
        notify_added_anti_skip(
            lesson,
            new_meta.get(lesson.exact_key, fallback_meta(lesson)),
            now,
            reminded,
            anti_skip_state,
        )

    for before, after in modified:
        # Un cambio di programmazione invalida una vecchia conferma/snooze:
        # vogliamo che il nuovo orario venga visto esplicitamente.
        clear_lesson_interactions(before, anti_skip_state)
        notify_modified_anti_skip(
            before,
            after,
            old_meta.get(before.exact_key, fallback_meta(before)),
            new_meta.get(after.exact_key, fallback_meta(after)),
            now,
            reminded,
            anti_skip_state,
        )

    for lesson in confirmed_removed:
        clear_lesson_interactions(lesson, anti_skip_state)
        core.notify(
            "🗑️ DIDATTICA RIMOSSA",
            lesson_card(lesson, old_meta.get(lesson.exact_key)),
        )

    for old_lesson, new_lesson, om, nm, certain in recoveries:
        notify_recovery_anti_skip(
            old_lesson,
            new_lesson,
            om,
            nm,
            certain,
            now,
            reminded,
            anti_skip_state,
        )

    # Calendar: i pending e le transizioni restano finché necessario.
    calendar_lessons = unique_lessons(
        list(new_lessons) + pending_preserved + transitions
    )
    calendar_added = added + [x[1] for x in recoveries]

    calendar_events = core.sync_calendar(
        old_lessons,
        calendar_lessons,
        calendar_events,
        calendar_added,
        confirmed_removed + silent_calendar_removed,
        modified,
    )

    # Migrazione una tantum degli eventi esistenti:
    # 24h, 2h, 30m e 10m.
    ensure_calendar_safety_reminders(
        calendar_lessons,
        calendar_events,
        anti_skip_state,
    )

    safety_lessons = unique_lessons(
        list(new_lessons) + pending_preserved + transitions
    )

    old_exact_keys = {x.exact_key for x in old_lessons}
    farthest = max(TELEGRAM_REMINDER_MINUTES)

    for lesson in safety_lessons:
        if (
            lesson.exact_key in old_exact_keys
            and lesson.reminder_key in reminded
        ):
            reminded.add(reminder_state_key(lesson, farthest))

    # 🌙 Sera prima.
    tomorrow = now.date() + timedelta(days=1)
    for lesson in safety_lessons:
        key = anti_skip_state_key(lesson, "day-before")
        if (
            lesson.start_dt > now
            and lesson.start_dt.date() == tomorrow
            and now.hour >= DAY_BEFORE_HOUR
            and key not in reminded
            and not acknowledgement_is_recent(
                lesson,
                anti_skip_state,
                now,
            )
        ):
            notify_lesson(
                "🌙 DIDATTICA DOMANI",
                lesson_card(
                    lesson,
                    lesson_meta_for(lesson, new_meta, old_meta),
                ),
                lesson,
                anti_skip_state,
                now,
            )
            reminded.add(key)

    # ☀️ Mattina stessa.
    for lesson in safety_lessons:
        key = anti_skip_state_key(lesson, "same-day")
        if (
            lesson.start_dt > now
            and lesson.start_dt.date() == now.date()
            and now.hour >= SAME_DAY_HOUR
            and key not in reminded
            and not acknowledgement_is_recent(
                lesson,
                anti_skip_state,
                now,
            )
        ):
            notify_lesson(
                "☀️ DIDATTICA OGGI",
                lesson_card(
                    lesson,
                    lesson_meta_for(lesson, new_meta, old_meta),
                ),
                lesson,
                anti_skip_state,
                now,
            )
            reminded.add(key)

    # ⏰ Promemoria richiesti manualmente con il pulsante Telegram.
    process_due_snoozes(
        safety_lessons,
        {
            lesson.exact_key: lesson_meta_for(
                lesson,
                new_meta,
                old_meta,
            )
            for lesson in safety_lessons
        },
        anti_skip_state,
        reminded,
        now,
    )

    # ⏰ 2h + 45m + 15m + 5m.
    for lesson in safety_lessons:
        minutes = (lesson.start_dt - now).total_seconds() / 60
        if minutes <= 0:
            continue

        acknowledged_recently = acknowledgement_is_recent(
            lesson,
            anti_skip_state,
            now,
        )
        due = [
            threshold
            for threshold in TELEGRAM_REMINDER_MINUTES
            if (
                minutes <= threshold
                and reminder_state_key(lesson, threshold) not in reminded
                and (
                    not acknowledged_recently
                    or threshold in ACK_KEEP_REMINDERS
                )
            )
        ]
        if not due:
            continue

        chosen = min(due)
        notify_lesson(
            threshold_title(chosen),
            reminder_card(
                lesson,
                minutes,
                lesson_meta_for(lesson, new_meta, old_meta),
            ),
            lesson,
            anti_skip_state,
            now,
            urgent=chosen <= 5,
        )

        for threshold in due:
            reminded.add(reminder_state_key(lesson, threshold))

    # Individua subito un'eventuale lezione programmata che dovrebbe essere
    # realmente in corso in questo momento.
    current_candidates = [
        lesson
        for lesson in safety_lessons
        if lesson.start_dt <= now < lesson.end_dt
    ]

    live_time_lesson = (
        max(current_candidates, key=lambda x: x.start_dt)
        if current_candidates
        else None
    )

    live_identity_lesson = match_live_identity_lesson(
        safety_lessons,
        new_meta,
        old_meta,
        live_evidence,
    )

    # Per mostrare materia/titolo possiamo usare anche l'identità letta
    # direttamente dalla scheda In corso. La conferma immediata, però,
    # continua a dipendere solo dalla compatibilità con l'orario.
    live_lesson = (
        live_time_lesson
        or live_identity_lesson
    )

    # ------------------------------------------------------------------
    # CONFERMA TRA RUN PER "IN CORSO" GENERICO
    # ------------------------------------------------------------------
    # Se Mercatorum dice soltanto che qualcosa è "In corso", ma non riusciamo
    # a collegarlo a una lezione programmata nell'orario attuale, non mandiamo
    # subito Telegram. Richiediamo una seconda rilevazione in un monitor
    # successivo. Un vero live associabile resta invece immediato.
    generic_candidate_key = "live_generic_candidate"
    generic_live_confirmed = False

    if live_present is True and live_time_lesson is None:
        candidate = anti_skip_state.get(generic_candidate_key)
        count = 1
        first_seen = now

        if isinstance(candidate, dict):
            try:
                previous_last_seen = datetime.fromisoformat(
                    str(candidate.get("last_seen", ""))
                )
                if previous_last_seen.tzinfo is None:
                    previous_last_seen = previous_last_seen.replace(
                        tzinfo=core.TIMEZONE
                    )

                gap = now - previous_last_seen

                if gap <= timedelta(
                    minutes=GENERIC_LIVE_MAX_GAP_MINUTES
                ):
                    count = int(candidate.get("count", 0) or 0) + 1

                    try:
                        first_seen = datetime.fromisoformat(
                            str(candidate.get("first_seen", ""))
                        )
                        if first_seen.tzinfo is None:
                            first_seen = first_seen.replace(
                                tzinfo=core.TIMEZONE
                            )
                    except Exception:
                        first_seen = previous_last_seen

            except Exception:
                count = 1
                first_seen = now

        anti_skip_state[generic_candidate_key] = {
            "count": count,
            "first_seen": first_seen.isoformat(),
            "last_seen": now.isoformat(),
        }

        # Se un live era già stato confermato in precedenza e Mercatorum
        # continua a mostrarlo, non lo rimettiamo in quarantena.
        generic_live_confirmed = (
            bool(anti_skip_state.get("live_since"))
            or count >= GENERIC_LIVE_CONFIRM_CHECKS
        )

        if not generic_live_confirmed:
            print(
                "Segnale 'In corso' generico in attesa di conferma "
                f"({count}/{GENERIC_LIVE_CONFIRM_CHECKS})."
            )

    if live_present is True and live_time_lesson is not None:
        # Una lezione prevista è effettivamente nell'intervallo orario:
        # questo è il caso ad alta confidenza e resta immediato.
        generic_live_confirmed = False
        anti_skip_state.pop(generic_candidate_key, None)

    if live_present is False:
        anti_skip_state.pop(generic_candidate_key, None)

    if live_present is None:
        # Un errore temporaneo nel controllo "In corso" non vale né come
        # conferma né come smentita. Eliminiamo però candidati ormai vecchi.
        candidate = anti_skip_state.get(generic_candidate_key)

        if isinstance(candidate, dict):
            try:
                last_seen = datetime.fromisoformat(
                    str(candidate.get("last_seen", ""))
                )
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(
                        tzinfo=core.TIMEZONE
                    )

                if now - last_seen > timedelta(
                    minutes=GENERIC_LIVE_MAX_GAP_MINUTES
                ):
                    anti_skip_state.pop(
                        generic_candidate_key,
                        None,
                    )
            except Exception:
                anti_skip_state.pop(
                    generic_candidate_key,
                    None,
                )

    live_confirmed = (
        live_present is True
        and (
            live_time_lesson is not None
            or generic_live_confirmed
        )
    )

    # Recupero se GitHub arriva appena dopo l'orario.
    # Un falso segnale generico "In corso" non deve più sopprimere questo
    # avviso di sicurezza.
    if not live_confirmed:
        for lesson in safety_lessons:
            elapsed = (now - lesson.start_dt).total_seconds() / 60
            key = anti_skip_state_key(lesson, "just-started")

            if (
                0 <= elapsed <= JUST_STARTED_GRACE_MINUTES
                and now < lesson.end_dt
                and key not in reminded
            ):
                notify_lesson(
                    "🚨 DIDATTICA APPENA INIZIATA",
                    (
                        lesson_card(
                            lesson,
                            lesson_meta_for(
                                lesson,
                                new_meta,
                                old_meta,
                            ),
                        )
                        + f"\n\n⚠️ È iniziata da circa **{human_minutes(elapsed)}**."
                    ),
                    lesson,
                    anti_skip_state,
                    now,
                    urgent=True,
                )
                reminded.add(key)

    # 🔴 In corso + richiami 10/20 minuti.
    live_follow_keys = {
        minutes: f"{LIVE_STATE_KEY}-follow-up-{minutes}"
        for minutes in LIVE_FOLLOW_UP_MINUTES
    }

    if live_confirmed:
        live_since_raw = anti_skip_state.get("live_since")

        if not live_since_raw:
            anti_skip_state["live_since"] = now.isoformat()
            live_since = now

        if live_since_raw:
            try:
                live_since = datetime.fromisoformat(live_since_raw)
                if live_since.tzinfo is None:
                    live_since = live_since.replace(
                        tzinfo=core.TIMEZONE
                    )
            except Exception:
                live_since = now
                anti_skip_state["live_since"] = now.isoformat()

        if LIVE_STATE_KEY not in reminded:
            if live_lesson is not None:
                notify_lesson(
                    "🔴 DIDATTICA IN DIRETTA",
                    (
                        lesson_card(
                            live_lesson,
                            lesson_meta_for(
                                live_lesson,
                                new_meta,
                                old_meta,
                            ),
                        )
                        + "\n\nMercatorum la segnala nello stato **In corso**."
                        + "\n▶️ È disponibile adesso sulla piattaforma."
                    ),
                    live_lesson,
                    anti_skip_state,
                    now,
                    urgent=True,
                )

            if live_lesson is None:
                notify_urgent_generic(
                    "🔴 DIDATTICA IN DIRETTA",
                    (
                        'Mercatorum segnala una didattica nello stato "In corso".\n\n'
                        "▶️ Il segnale è stato confermato in due controlli "
                        "separati. È disponibile adesso sulla piattaforma."
                    ),
                    anti_skip_state,
                )

            reminded.add(LIVE_STATE_KEY)

        live_elapsed = (
            now - live_since
        ).total_seconds() / 60

        live_acknowledged = (
            live_lesson is not None
            and acknowledgement_is_recent(
                live_lesson,
                anti_skip_state,
                now,
            )
        )

        for threshold in LIVE_FOLLOW_UP_MINUTES:
            key = live_follow_keys[threshold]

            if (
                live_elapsed >= threshold
                and key not in reminded
                and not live_acknowledged
            ):
                body = (
                    "Mercatorum segnala ancora la didattica come "
                    "**In corso**.\n\n"
                    "▶️ Se non sei già collegato, controlla subito "
                    "la piattaforma."
                )

                if live_lesson is not None:
                    notify_lesson(
                        f"🚨 DIDATTICA ANCORA IN CORSO · {threshold} MIN",
                        (
                            lesson_card(
                                live_lesson,
                                lesson_meta_for(
                                    live_lesson,
                                    new_meta,
                                    old_meta,
                                ),
                            )
                            + "\n\n"
                            + body
                        ),
                        live_lesson,
                        anti_skip_state,
                        now,
                        urgent=True,
                    )

                if live_lesson is None:
                    notify_urgent_generic(
                        f"🚨 DIDATTICA ANCORA IN CORSO · {threshold} MIN",
                        body,
                        anti_skip_state,
                    )

                reminded.add(key)

    if live_present is False:
        reminded.discard(LIVE_STATE_KEY)

        for key in live_follow_keys.values():
            reminded.discard(key)

        anti_skip_state.pop("live_since", None)
    # Conserva solo chiavi relative a lezioni attive.
    active_reminders: set[str] = set()

    for lesson in safety_lessons:
        for threshold in TELEGRAM_REMINDER_MINUTES:
            active_reminders.add(reminder_state_key(lesson, threshold))
        active_reminders.add(anti_skip_state_key(lesson, "day-before"))
        active_reminders.add(anti_skip_state_key(lesson, "same-day"))
        active_reminders.add(anti_skip_state_key(lesson, "just-started"))

    if live_confirmed or anti_skip_state.get("live_since"):
        active_reminders.add(LIVE_STATE_KEY)
        active_reminders.update(live_follow_keys.values())

    reminded.intersection_update(active_reminders)

    cleanup_interaction_state(
        safety_lessons,
        anti_skip_state,
    )

    state_lessons = safety_lessons

    state_meta: dict[str, dict] = {}
    for lesson in state_lessons:
        state_meta[lesson.exact_key] = lesson_meta_for(
            lesson,
            new_meta,
            old_meta,
        )

    save_extended_state(
        state_lessons,
        reminded,
        calendar_events,
        pending_missing,
        removed_history,
        state_meta,
        anti_skip_state,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
