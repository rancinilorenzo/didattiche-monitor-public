from __future__ import annotations

import hashlib
import html
import json
import os
import re
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

# Anti-Salto v1.
MANDATORY_TELEGRAM_REMINDERS = {120, 45, 15, 5}
DAY_BEFORE_HOUR = int(os.getenv("ANTI_SKIP_DAY_BEFORE_HOUR", "19"))
SAME_DAY_HOUR = int(os.getenv("ANTI_SKIP_SAME_DAY_HOUR", "8"))
JUST_STARTED_GRACE_MINUTES = int(
    os.getenv("ANTI_SKIP_JUST_STARTED_GRACE_MINUTES", "12")
)
LIVE_FOLLOW_UP_MINUTES = (10, 20)

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


def telegram_notify_rich(title: str, body: str) -> None:
    """
    Override solo del rendering Telegram.
    Non cambia diff, stato, Calendar, reminder o logica delle notifiche.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    rendered_title = f"<b>{html.escape(title)}</b>"
    rendered_body = telegram_html(body)
    text = f"{rendered_title}\n\n{rendered_body}"

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


# core.notify risolve telegram_notify al momento della chiamata:
# sostituendolo qui manteniamo invariata tutta la logica esistente.
core.telegram_notify = telegram_notify_rich


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

    core.notify(title, body)


def notify_modified_anti_skip(
    before: core.Lesson,
    after: core.Lesson,
    before_meta: dict,
    after_meta: dict,
    now: datetime,
    reminded: set[str],
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

    core.notify(title, body)


def notify_recovery_anti_skip(
    old_lesson: core.Lesson,
    new_lesson: core.Lesson,
    old_meta: dict,
    new_meta: dict,
    certain: bool,
    now: datetime,
    reminded: set[str],
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

    core.notify(title, body)


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


def detect_live_present(page) -> bool:
    click_live_tab(page)
    first = page_live_status(page)
    if not first:
        return False

    page.wait_for_timeout(3000)
    return page_live_status(page)


# ---------------------------------------------------------------------------
# Login + snapshot
# ---------------------------------------------------------------------------

def scrape_snapshot() -> tuple[list[core.Lesson], dict[str, dict], bool | None]:
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
                live_present = detect_live_present(page)
            except Exception as exc:
                print(
                    "Controllo 'In corso' non riuscito: "
                    f"{type(exc).__name__}"
                )
                live_present = None

            core.best_effort_logout(page)
            return lessons, meta_by_key, live_present

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

    new_lessons, new_meta, live_present = scrape_snapshot()

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
        )

    for before, after in modified:
        notify_modified_anti_skip(
            before,
            after,
            old_meta.get(before.exact_key, fallback_meta(before)),
            new_meta.get(after.exact_key, fallback_meta(after)),
            now,
            reminded,
        )

    for lesson in confirmed_removed:
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
        ):
            core.notify(
                "🌙 DIDATTICA DOMANI",
                lesson_card(
                    lesson,
                    lesson_meta_for(lesson, new_meta, old_meta),
                ),
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
        ):
            core.notify(
                "☀️ DIDATTICA OGGI",
                lesson_card(
                    lesson,
                    lesson_meta_for(lesson, new_meta, old_meta),
                ),
            )
            reminded.add(key)

    # ⏰ 2h + 45m + 15m + 5m.
    for lesson in safety_lessons:
        minutes = (lesson.start_dt - now).total_seconds() / 60
        if minutes <= 0:
            continue

        due = [
            threshold
            for threshold in TELEGRAM_REMINDER_MINUTES
            if (
                minutes <= threshold
                and reminder_state_key(lesson, threshold) not in reminded
            )
        ]
        if not due:
            continue

        chosen = min(due)
        core.notify(
            threshold_title(chosen),
            reminder_card(
                lesson,
                minutes,
                lesson_meta_for(lesson, new_meta, old_meta),
            ),
        )

        for threshold in due:
            reminded.add(reminder_state_key(lesson, threshold))

    # Recupero se GitHub arriva appena dopo l'orario.
    if live_present is not True:
        for lesson in safety_lessons:
            elapsed = (now - lesson.start_dt).total_seconds() / 60
            key = anti_skip_state_key(lesson, "just-started")
            if (
                0 <= elapsed <= JUST_STARTED_GRACE_MINUTES
                and now < lesson.end_dt
                and key not in reminded
            ):
                core.notify(
                    "🚨 DIDATTICA APPENA INIZIATA",
                    (
                        lesson_card(
                            lesson,
                            lesson_meta_for(lesson, new_meta, old_meta),
                        )
                        + f"\n\n⚠️ È iniziata da circa **{human_minutes(elapsed)}**."
                    ),
                )
                reminded.add(key)

    # 🔴 In corso + richiami 10/20 minuti.
    live_follow_keys = {
        minutes: f"{LIVE_STATE_KEY}-follow-up-{minutes}"
        for minutes in LIVE_FOLLOW_UP_MINUTES
    }

    if live_present is True:
        live_since_raw = anti_skip_state.get("live_since")
        if not live_since_raw:
            anti_skip_state["live_since"] = now.isoformat()
            live_since = now
        else:
            try:
                live_since = datetime.fromisoformat(live_since_raw)
                if live_since.tzinfo is None:
                    live_since = live_since.replace(tzinfo=core.TIMEZONE)
            except Exception:
                live_since = now
                anti_skip_state["live_since"] = now.isoformat()

        if LIVE_STATE_KEY not in reminded:
            core.notify(
                "🔴 DIDATTICA IN DIRETTA",
                (
                    'Mercatorum segnala una didattica nello stato "In corso".\n\n'
                    "▶️ È disponibile adesso sulla piattaforma."
                ),
            )
            reminded.add(LIVE_STATE_KEY)

        live_elapsed = (now - live_since).total_seconds() / 60
        for threshold in LIVE_FOLLOW_UP_MINUTES:
            key = live_follow_keys[threshold]
            if live_elapsed >= threshold and key not in reminded:
                core.notify(
                    f"🚨 DIDATTICA ANCORA IN CORSO · {threshold} MIN",
                    (
                        "Mercatorum segnala ancora la didattica come **In corso**.\n\n"
                        "▶️ Se non sei già collegato, controlla subito la piattaforma."
                    ),
                )
                reminded.add(key)

    elif live_present is False:
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

    if live_present is True or anti_skip_state.get("live_since"):
        active_reminders.add(LIVE_STATE_KEY)
        active_reminders.update(live_follow_keys.values())

    reminded.intersection_update(active_reminders)

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
