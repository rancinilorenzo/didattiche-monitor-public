from __future__ import annotations

import hashlib
import json
import os
import re
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
    raw = os.getenv("TELEGRAM_REMINDER_MINUTES", "45,5")
    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError(
                "TELEGRAM_REMINDER_MINUTES deve contenere minuti interi "
                "separati da virgola, es. 45,5"
            ) from exc
        if value > 0:
            values.add(value)

    if not values:
        raise RuntimeError(
            "TELEGRAM_REMINDER_MINUTES non contiene alcun valore valido."
        )

    return sorted(values, reverse=True)


TELEGRAM_REMINDER_MINUTES = parse_telegram_reminder_minutes()


def normalize_identity(value: str) -> str:
    value = core.normalize_space(value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return core.normalize_space(value)


def reminder_state_key(lesson: core.Lesson, threshold: int) -> str:
    value = f"{lesson.exact_key}|telegram-reminder|{threshold}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


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
        lines.append(subject)
    if title:
        lines.append(title)
    elif not subject:
        lines.append(lesson_name(lesson))

    lines += [
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

    lines = [header, ""]
    subject = core.normalize_space(after_meta.get("subject", ""))
    title = core.normalize_space(after_meta.get("title", ""))

    if subject:
        lines.append(subject)
    if title:
        lines.append(title)
    if not subject and not title:
        lines.append(lesson_name(after))

    if date_changed or time_changed:
        lines += [
            "",
            f"Prima: {fmt_date(before.date)} · {before.start}–{before.end}",
            f"Ora:   {fmt_date(after.date)} · {after.start}–{after.end}",
        ]

    old_label = " · ".join(
        x for x in [
            core.normalize_space(before_meta.get("subject", "")),
            core.normalize_space(before_meta.get("title", "")),
        ] if x
    )
    new_label = " · ".join(
        x for x in [subject, title] if x
    )
    if old_label and new_label and old_label != new_label:
        lines += ["", f"Prima: {old_label}", f"Ora:   {new_label}"]

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
        lines.append(subject)
    if title:
        lines.append(title)
    if not subject and not title:
        lines.append(lesson_name(new_lesson))

    lines += [
        "",
        "Vecchia programmazione:",
        f"📅 {fmt_date(old_lesson.date)}",
        f"🕒 {old_lesson.start}–{old_lesson.end}",
        "",
        "Nuova programmazione:",
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
) -> None:
    functional = {
        "events": [asdict(x) for x in lessons],
        "reminded": sorted(reminded),
        "calendar_events": calendar_events,
        "pending_missing": pending_missing,
        "removed_history": removed_history,
        "lesson_meta": lesson_meta,
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

    # Pulizia silenziosa di eventuali righe sporche salvate da versioni precedenti
    # (es. footer/banner cookie inglobato nel titolo). Non generiamo notifiche false,
    # ma le passiamo al sync Calendar per eliminare eventuali eventi duplicati/orfani.
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

    # Ulteriore sicurezza: una riga sporca non entra mai nel nuovo snapshot.
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

    # Pulisci storico vecchio.
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
        old_future = [x for x in old_lessons if x.start_dt > now]
        new_future = [x for x in new_lessons if x.start_dt > now]

        added, raw_removed, modified = core.diff_lessons(
            old_future,
            new_future,
        )
        candidates, transitions = filter_live_transitions(
            raw_removed,
            now,
            live_present,
        )

        current_keys = {x.exact_key for x in new_lessons}

        # Riapparsa uguale o riconosciuta come modifica: annulla il pending.
        for key in list(pending_missing):
            if key in current_keys:
                pending_missing.pop(key, None)

        for before, _ in modified:
            pending_missing.pop(before.exact_key, None)

        candidate_keys = {x.exact_key for x in candidates}

        # Primo/secondo/terzo check della rimozione.
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

        # Pending già esistenti: se ancora futuri e ancora assenti,
        # devono continuare a contare anche se non sono rientrati nel diff.
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

    # Possibili recuperi/riprogrammazioni.
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

    # Notifiche: una didattica = un messaggio Telegram separato.
    for lesson in added:
        core.notify(
            "🆕 DIDATTICA AGGIUNTA",
            lesson_card(lesson, new_meta.get(lesson.exact_key)),
        )

    for before, after in modified:
        message = modified_card(
            before,
            after,
            old_meta.get(before.exact_key),
            new_meta.get(after.exact_key),
        )
        title, _, body = message.partition("\n")
        core.notify(title, body.lstrip())

    for lesson in confirmed_removed:
        core.notify(
            "🗑️ DIDATTICA RIMOSSA",
            lesson_card(lesson, old_meta.get(lesson.exact_key)),
        )

    for old_lesson, new_lesson, om, nm, certain in recoveries:
        message = recovery_card(
            old_lesson,
            new_lesson,
            om,
            nm,
            certain,
        )
        title, _, body = message.partition("\n")
        core.notify(title, body.lstrip())

    # Calendar: i pending restano finché la rimozione non è confermata.
    calendar_lessons = list(new_lessons)
    known_calendar = {x.exact_key for x in calendar_lessons}

    for lesson in pending_preserved + transitions:
        if lesson.exact_key not in known_calendar:
            calendar_lessons.append(lesson)
            known_calendar.add(lesson.exact_key)

    # I recuperi sono nuove lezioni anche se la notifica non usa "AGGIUNTA".
    calendar_added = added + [x[1] for x in recoveries]

    calendar_events = core.sync_calendar(
        old_lessons,
        calendar_lessons,
        calendar_events,
        calendar_added,
        confirmed_removed + silent_calendar_removed,
        modified,
    )

    # Migrazione reminder vecchio -> nuovo.
    old_exact_keys = {x.exact_key for x in old_lessons}
    farthest = max(TELEGRAM_REMINDER_MINUTES)

    for lesson in new_lessons:
        if (
            lesson.exact_key in old_exact_keys
            and lesson.reminder_key in reminded
        ):
            reminded.add(
                reminder_state_key(lesson, farthest)
            )

    # Reminder 45 + 5: ogni lezione genera un messaggio separato.
    for lesson in new_lessons:
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

        # Se il monitor vede per la prima volta una lezione quando sono già
        # state superate più soglie, invia solo quella più vicina all'inizio.
        chosen = min(due)
        core.notify(
            f"⏰ DIDATTICA IN ARRIVO · {chosen} MIN",
            reminder_card(
                lesson,
                minutes,
                new_meta.get(lesson.exact_key),
            ),
        )

        for threshold in due:
            reminded.add(reminder_state_key(lesson, threshold))

    # In corso.
    if live_present is True and LIVE_STATE_KEY not in reminded:
        core.notify(
            "🔴 DIDATTICA IN DIRETTA",
            (
                "Mercatorum segnala una didattica nello stato \"In corso\".\n\n"
                "▶️ È disponibile adesso sulla piattaforma."
            ),
        )
        reminded.add(LIVE_STATE_KEY)
    elif live_present is False:
        reminded.discard(LIVE_STATE_KEY)

    active_reminders = {
        reminder_state_key(lesson, threshold)
        for lesson in new_lessons
        for threshold in TELEGRAM_REMINDER_MINUTES
    }
    if LIVE_STATE_KEY in reminded:
        active_reminders.add(LIVE_STATE_KEY)

    reminded.intersection_update(active_reminders)

    # Stato: conserva le lezioni in pending e le transizioni naturali.
    state_lessons = list(new_lessons)
    state_keys = {x.exact_key for x in state_lessons}

    for lesson in pending_preserved + transitions:
        if lesson.exact_key not in state_keys:
            state_lessons.append(lesson)
            state_keys.add(lesson.exact_key)

    state_meta: dict[str, dict] = {}
    for lesson in state_lessons:
        state_meta[lesson.exact_key] = (
            new_meta.get(lesson.exact_key)
            or old_meta.get(lesson.exact_key)
            or fallback_meta(lesson)
        )

    save_extended_state(
        state_lessons,
        reminded,
        calendar_events,
        pending_missing,
        removed_history,
        state_meta,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
