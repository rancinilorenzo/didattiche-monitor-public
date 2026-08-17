from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SCHEDULE_URL = os.getenv(
    "MERCATORUM_SCHEDULE_URL",
    "https://lms.mercatorum.multiversity.click/class?caller=scheduled",
)
USERNAME = os.getenv("MERCATORUM_USERNAME", "")
PASSWORD = os.getenv("MERCATORUM_PASSWORD", "")
TIMEZONE_NAME = os.getenv("TZ", "Europe/Rome")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
STATE_PATH = Path(os.getenv("STATE_PATH", "state.enc"))
LEGACY_STATE_PATH = Path(os.getenv("LEGACY_STATE_PATH", "state.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "45"))
CALENDAR_REMINDERS = [60, 15]

MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

WEEKDAYS = r"(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)"
MONTH_NAMES = "|".join(MONTHS)
DATE_RE = re.compile(
    rf"(?P<day>\d{{1,2}})\s+(?P<weekday>{WEEKDAYS})\s+"
    rf"(?P<month>{MONTH_NAMES})\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\bInizio\s*(?P<start>\d{1,2}:\d{2}).*?\bFine\s*(?P<end>\d{1,2}:\d{2})",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Lesson:
    date: str
    start: str
    end: str
    description: str

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(f"{self.date}T{self.start}:00").replace(tzinfo=TIMEZONE)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromisoformat(f"{self.date}T{self.end}:00").replace(tzinfo=TIMEZONE)

    @property
    def exact_key(self) -> str:
        return _sha(f"{self.date}|{self.start}|{self.end}|{self.description}")

    @property
    def reminder_key(self) -> str:
        return _sha(f"{self.date}|{self.description}")


def _sha(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_lessons(body_text: str) -> list[Lesson]:
    text = normalize_space(body_text)
    matches = list(DATE_RE.finditer(text))
    lessons: list[Lesson] = []

    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():block_end]
        time_match = TIME_RE.search(block)
        if not time_match:
            continue

        day = int(match.group("day"))
        month = MONTHS[match.group("month").lower()]
        year = int(match.group("year"))
        date_iso = f"{year:04d}-{month:02d}-{day:02d}"
        start = time_match.group("start")
        end = time_match.group("end")

        description = normalize_space(block[time_match.end():])
        if not description:
            description = normalize_space(TIME_RE.sub("", block, count=1))
        if not description:
            description = "Didattica sincrona"

        lessons.append(Lesson(date_iso, start, end, description))

    seen: set[str] = set()
    unique: list[Lesson] = []
    for lesson in lessons:
        if lesson.exact_key not in seen:
            seen.add(lesson.exact_key)
            unique.append(lesson)
    return unique


def first_visible(page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def login_if_needed(page) -> None:
    """Esegue il login e salva diagnostica utile senza mai scrivere le credenziali."""
    password_field = first_visible(page, ["#password", "input[type='password']"])
    if not password_field:
        return

    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Il portale richiede il login, ma MERCATORUM_USERNAME/MERCATORUM_PASSWORD non sono configurati."
        )

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    network_events: list[dict] = []
    console_events: list[str] = []

    def record_response(response) -> None:
        try:
            url = response.url
            low = url.lower()
            safe_url = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
            # Conserva solo richieste probabilmente rilevanti o errori HTTP.
            if response.status >= 400 or any(k in low for k in ("login", "auth", "signin", "token", "/api/")):
                network_events.append(
                    {
                        "status": response.status,
                        "method": response.request.method,
                        "url": safe_url,
                    }
                )
        except Exception:
            pass

    def record_console(msg) -> None:
        try:
            if msg.type in ("error", "warning"):
                console_events.append(f"{msg.type}: {msg.text}")
        except Exception:
            pass

    page.on("response", record_response)
    page.on("console", record_console)

    username_field = first_visible(
        page,
        [
            "#username",
            "input[type='email']",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[name*='login' i]",
            "input[id*='login' i]",
            "input[name*='matricola' i]",
            "input[id*='matricola' i]",
            "input[type='text']",
        ],
    )
    if not username_field:
        raise RuntimeError("Non trovo il campo username nella pagina di login.")

    username_field.fill(USERNAME)
    password_field.fill(PASSWORD)

    # Verifica che Playwright abbia davvero valorizzato i campi, senza loggarne il contenuto.
    if not username_field.input_value():
        raise RuntimeError("Il campo username è rimasto vuoto dopo fill().")
    if not password_field.input_value():
        raise RuntimeError("Il campo password è rimasto vuoto dopo fill().")

    before_url = page.url
    submit = first_visible(
        page,
        [
            "button:has-text('Accedi')",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Entra')",
        ],
    )
    if submit:
        submit.click()
    else:
        password_field.press("Enter")

    # Il login è gestito da una SPA: non affidiamoci solo a networkidle.
    # Aspettiamo che il campo password sparisca/non sia più visibile oppure che cambi URL.
    try:
        page.wait_for_function(
            """(oldUrl) => {
                const p = document.querySelector('#password, input[type="password"]');
                const hidden = !p || p.offsetParent === null;
                return hidden || window.location.href !== oldUrl;
            }""",
            arg=before_url,
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        pass

    # Lascia terminare eventuali chiamate XHR / aggiornamenti del router.
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1_500)

    try:
        storage = page.evaluate(
            """() => ({
                localStorageKeys: Object.keys(localStorage),
                sessionStorageKeys: Object.keys(sessionStorage),
                url: location.href
            })"""
        )
    except Exception:
        storage = {"url": page.url}

    try:
        cookie_names = [c.get("name") for c in page.context.cookies()]
    except Exception:
        cookie_names = []

    diag = {
        "before_url": before_url,
        "after_url": page.url,
        "storage": storage,
        "cookie_names": cookie_names,
        "network": network_events[-50:],
        "console": console_events[-50:],
    }
    (DEBUG_DIR / "login-debug.json").write_text(
        json.dumps(diag, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Se siamo ancora sulla schermata di login, salva anche il testo visibile.
    if first_visible(page, ["#password", "input[type='password']"]):
        try:
            body_text = page.locator("body").inner_text(timeout=5_000)
            (DEBUG_DIR / "login-body.txt").write_text(body_text, encoding="utf-8")
        except Exception:
            pass

        # Riassunto sicuro delle risposte HTTP: niente body e niente credenziali.
        failures = [e for e in network_events if e.get("status", 0) >= 400]
        if failures:
            last = failures[-1]
            raise RuntimeError(
                f"Login non riuscito: il portale è ancora sulla pagina di accesso. "
                f"Ultima risposta HTTP anomala: {last['status']} {last['method']} {last['url']}"
            )
        raise RuntimeError(
            "Login non riuscito: il portale è ancora sulla pagina di accesso. "
            "Controlla debug/login-debug.json e debug/login-body.txt nell'artifact."
        )


def best_effort_logout(page) -> None:
    for selector in [
        "a:has-text('Logout')",
        "button:has-text('Logout')",
        "a:has-text('Esci')",
        "button:has-text('Esci')",
        "a:has-text('Disconnetti')",
    ]:
        locator = page.locator(selector)
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=2_000)
                return
        except Exception:
            continue


def settle_spa(page, extra_wait_ms: int = 1500) -> None:
    """Dà tempo alla SPA di completare redirect/router e rendering del form."""
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(extra_wait_ms)


def scrape_lessons() -> list[Lesson]:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(locale="it-IT", timezone_id=TIMEZONE_NAME)
        page = context.new_page()
        try:
            # Mercatorum è una SPA: subito dopo domcontentloaded il form di login
            # può non essere ancora renderizzato. Aspettiamo il router prima di
            # decidere se serve autenticarsi.
            page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=45_000)
            settle_spa(page, 2000)
            login_if_needed(page)

            # Dopo il login apriamo esplicitamente la pagina delle didattiche.
            page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=45_000)
            settle_spa(page, 2000)

            # Se il router ci ha rimandati al login (o il form è apparso solo ora),
            # eseguiamo un secondo tentativo controllato. In caso di fallimento
            # login_if_needed() salva login-debug.json e login-body.txt.
            if first_visible(page, ["#password", "input[type='password']"]):
                login_if_needed(page)
                page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=45_000)
                settle_spa(page, 2000)

            if first_visible(page, ["#password", "input[type='password']"]):
                raise RuntimeError(
                    "Login non riuscito: dopo il tentativo di autenticazione "
                    "Mercatorum mostra ancora la pagina di accesso."
                )

            body_text = page.locator("body").inner_text(timeout=15_000)
            lessons = parse_lessons(body_text)
            if not lessons:
                page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
                (DEBUG_DIR / "page.txt").write_text(body_text, encoding="utf-8")
                (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "Nessuna didattica sincrona trovata. Ho salvato diagnostica in debug/."
                )

            best_effort_logout(page)
            return lessons
        except Exception:
            try:
                page.screenshot(path=str(DEBUG_DIR / "failure.png"), full_page=True)
                (DEBUG_DIR / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


def empty_state() -> dict:
    return {"events": [], "reminded": [], "calendar_events": {}}


def state_fernet() -> Fernet:
    """Costruisce il cifratore usando una chiave Base64 da 32 byte nei Secrets."""
    encoded = os.getenv("STATE_ENCRYPTION_KEY", "").strip()
    if not encoded:
        raise RuntimeError(
            "STATE_ENCRYPTION_KEY non è configurata. Aggiungila nei GitHub Actions Secrets."
        )
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("STATE_ENCRYPTION_KEY non è una chiave Base64 valida.") from exc
    if len(raw) != 32:
        raise RuntimeError("STATE_ENCRYPTION_KEY deve rappresentare esattamente 32 byte.")
    return Fernet(base64.urlsafe_b64encode(raw))


def normalize_state(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Lo stato non è un oggetto JSON valido")
    data.setdefault("events", [])
    data.setdefault("reminded", [])
    data.setdefault("calendar_events", {})
    return data


def load_state() -> dict:
    # Stato nuovo: sempre cifrato. Se esiste ma la chiave è errata, fermiamo il job
    # invece di ripartire da zero e rischiare duplicati nel calendario.
    if STATE_PATH.exists():
        try:
            encrypted = STATE_PATH.read_bytes()
            plaintext = state_fernet().decrypt(encrypted)
            return normalize_state(json.loads(plaintext.decode("utf-8")))
        except InvalidToken as exc:
            raise RuntimeError(
                "Impossibile decifrare state.enc: STATE_ENCRYPTION_KEY non corrisponde alla chiave usata."
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Impossibile leggere lo stato cifrato {STATE_PATH}.") from exc

    # Migrazione una tantum dal vecchio state.json in chiaro. Serve a conservare
    # gli ID degli eventi Google Calendar già creati ed evitare duplicati.
    if LEGACY_STATE_PATH.exists():
        try:
            return normalize_state(json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            raise RuntimeError(
                "Il vecchio state.json esiste ma non è leggibile: non procedo per evitare duplicati."
            ) from exc

    return empty_state()


def save_state(lessons: list[Lesson], reminded: set[str], calendar_events: dict[str, str]) -> None:
    # Confrontiamo solo lo stato funzionale. Evitiamo di ricifrare ad ogni run: Fernet
    # usa un nonce casuale, quindi lo stesso contenuto produrrebbe comunque un file
    # diverso e causerebbe un commit Git ogni 5 minuti.
    functional = {
        "events": [asdict(item) for item in lessons],
        "reminded": sorted(reminded),
        "calendar_events": calendar_events,
    }

    existing: dict | None = None
    if STATE_PATH.exists():
        try:
            plaintext = state_fernet().decrypt(STATE_PATH.read_bytes())
            existing = normalize_state(json.loads(plaintext.decode("utf-8")))
        except InvalidToken as exc:
            raise RuntimeError(
                "Impossibile decifrare state.enc: STATE_ENCRYPTION_KEY non corrisponde alla chiave usata."
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Impossibile leggere lo stato cifrato {STATE_PATH}.") from exc

    if existing is not None:
        existing_functional = {
            "events": existing.get("events", []),
            "reminded": sorted(existing.get("reminded", [])),
            "calendar_events": existing.get("calendar_events", {}),
        }
        if existing_functional == functional:
            return

    payload = {
        "updated_at": datetime.now(TIMEZONE).isoformat(),
        **functional,
    }
    plaintext = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    encrypted = state_fernet().encrypt(plaintext)

    temp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp_path.write_bytes(encrypted)
    temp_path.replace(STATE_PATH)

    # Solo dopo avere scritto con successo lo stato cifrato eliminiamo il vecchio
    # file in chiaro. Il workflow committerà sia state.enc sia questa rimozione.
    if LEGACY_STATE_PATH.exists() and LEGACY_STATE_PATH.resolve() != STATE_PATH.resolve():
        LEGACY_STATE_PATH.unlink()


def lesson_from_dict(value: dict) -> Lesson:
    return Lesson(
        date=value["date"],
        start=value["start"],
        end=value["end"],
        description=value.get("description", "Didattica sincrona"),
    )


def similarity(a: Lesson, b: Lesson) -> float:
    return SequenceMatcher(None, a.description.lower(), b.description.lower()).ratio()


def diff_lessons(old: list[Lesson], new: list[Lesson]):
    old_by_exact = {item.exact_key: item for item in old}
    new_by_exact = {item.exact_key: item for item in new}

    removed = [v for k, v in old_by_exact.items() if k not in new_by_exact]
    added = [v for k, v in new_by_exact.items() if k not in old_by_exact]
    modified: list[tuple[Lesson, Lesson]] = []

    remaining_added = added[:]
    remaining_removed: list[Lesson] = []
    for old_item in removed:
        candidates = []
        for new_item in remaining_added:
            score = similarity(old_item, new_item)
            same_date_bonus = 0.12 if old_item.date == new_item.date else 0.0
            candidates.append((score + same_date_bonus, score, new_item))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if candidates and candidates[0][1] >= 0.80:
            new_item = candidates[0][2]
            modified.append((old_item, new_item))
            remaining_added.remove(new_item)
        else:
            remaining_removed.append(old_item)

    return remaining_added, remaining_removed, modified


def fmt_lesson(lesson: Lesson) -> str:
    date = datetime.fromisoformat(lesson.date).strftime("%d/%m/%Y")
    return f"{date} · {lesson.start}–{lesson.end} · {lesson.description}"


def build_change_message(added, removed, modified) -> str:
    lines: list[str] = []
    if added:
        lines.append("### 🆕 Nuove didattiche")
        lines.extend(f"- {fmt_lesson(x)}" for x in added)
    if removed:
        lines.append("### 🗑️ Didattiche rimosse")
        lines.extend(f"- {fmt_lesson(x)}" for x in removed)
    if modified:
        lines.append("### ✏️ Didattiche modificate")
        for before, after in modified:
            lines.append(f"- Prima: {fmt_lesson(before)}")
            lines.append(f"  Dopo: {fmt_lesson(after)}")
    return "\n".join(lines)


def plain_text(markdown: str) -> str:
    value = re.sub(r"[#*_`]", "", markdown)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def telegram_notify(title: str, body: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    text = f"{title}\n\n{plain_text(body)}"
    payload = json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def whatsapp_notify(title: str, body: str) -> None:
    """Send a WhatsApp alert with Twilio.

    For testing, free-form Body works only while a 24-hour user-initiated window is
    open. For unattended production alerts, set TWILIO_CONTENT_SID to an approved
    template such as: "Mercatorum: {{1}}".
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "")
    to_number = os.getenv("TWILIO_WHATSAPP_TO", "")
    content_sid = os.getenv("TWILIO_CONTENT_SID", "")
    if not sid or not token or not from_number or not to_number:
        return

    text = f"{title}\n\n{plain_text(body)}"[:1500]
    fields = {
        "From": from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}",
        "To": to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}",
    }
    if content_sid:
        fields["ContentSid"] = content_sid
        fields["ContentVariables"] = json.dumps({"1": text}, ensure_ascii=False)
    else:
        fields["Body"] = text

    data = urllib.parse.urlencode(fields).encode("utf-8")
    credentials = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "mercatorum-monitor",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def notify(title: str, body: str) -> None:
    # Non scriviamo nel log GitHub date, orari o titoli delle lezioni.
    # I dettagli restano disponibili solo sui canali privati configurati.
    print(f"{title} — notifica elaborata (dettagli omessi dal log).")
    for name, fn in [("Telegram", telegram_notify), ("WhatsApp", whatsapp_notify)]:
        try:
            fn(title, body)
        except Exception as exc:
            print(f"{name} notification failed: {type(exc).__name__}", file=sys.stderr)


def google_calendar_service():
    encoded = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64", "")
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "")
    if not encoded or not calendar_id:
        return None, None
    try:
        info = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_B64 non è un JSON service-account valido in Base64") from exc
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False), calendar_id


def calendar_body(lesson: Lesson) -> dict:
    title = normalize_space(lesson.description)
    if len(title) > 180:
        title = title[:177] + "…"
    return {
        "summary": f"🎓 Mercatorum · {title}",
        "description": (
            "Didattica sincrona Mercatorum.\n\n"
            "Evento creato e mantenuto automaticamente da Mercatorum Sync Monitor."
        ),
        "start": {"dateTime": lesson.start_dt.isoformat(), "timeZone": TIMEZONE_NAME},
        "end": {"dateTime": lesson.end_dt.isoformat(), "timeZone": TIMEZONE_NAME},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in CALENDAR_REMINDERS],
        },
        "extendedProperties": {
            "private": {
                "mercatorumExactKey": lesson.exact_key,
                "managedBy": "mercatorum-monitor",
            }
        },
    }


def calendar_insert(service, calendar_id: str, lesson: Lesson) -> str:
    event = service.events().insert(calendarId=calendar_id, body=calendar_body(lesson)).execute()
    return event["id"]


def calendar_update(service, calendar_id: str, event_id: str, lesson: Lesson) -> None:
    service.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=calendar_body(lesson),
    ).execute()


def calendar_delete(service, calendar_id: str, event_id: str) -> None:
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        if getattr(exc.resp, "status", None) not in {404, 410}:
            raise


def sync_calendar(
    old_lessons: list[Lesson],
    new_lessons: list[Lesson],
    calendar_events: dict[str, str],
    added: list[Lesson],
    removed: list[Lesson],
    modified: list[tuple[Lesson, Lesson]],
) -> dict[str, str]:
    service, calendar_id = google_calendar_service()
    if service is None:
        return calendar_events

    mapping = dict(calendar_events)

    # First activation (or calendar enabled later): create missing current lessons.
    if not old_lessons or not mapping:
        mapping = {}
        for lesson in new_lessons:
            mapping[lesson.exact_key] = calendar_insert(service, calendar_id, lesson)
        return mapping

    for before, after in modified:
        event_id = mapping.pop(before.exact_key, None)
        if event_id:
            try:
                calendar_update(service, calendar_id, event_id, after)
                mapping[after.exact_key] = event_id
                continue
            except HttpError as exc:
                if getattr(exc.resp, "status", None) not in {404, 410}:
                    raise
        mapping[after.exact_key] = calendar_insert(service, calendar_id, after)

    for lesson in removed:
        event_id = mapping.pop(lesson.exact_key, None)
        if event_id:
            calendar_delete(service, calendar_id, event_id)

    for lesson in added:
        if lesson.exact_key not in mapping:
            mapping[lesson.exact_key] = calendar_insert(service, calendar_id, lesson)

    # Preserve only IDs that correspond to the currently visible schedule.
    active = {lesson.exact_key for lesson in new_lessons}
    return {key: value for key, value in mapping.items() if key in active}


def main() -> int:
    state = load_state()
    old_lessons = [lesson_from_dict(x) for x in state.get("events", [])]
    reminded = set(state.get("reminded", []))
    calendar_events = dict(state.get("calendar_events", {}))

    new_lessons = scrape_lessons()
    now = datetime.now(TIMEZONE)

    added: list[Lesson] = []
    removed: list[Lesson] = []
    modified: list[tuple[Lesson, Lesson]] = []

    if not old_lessons:
        body = f"Monitor attivato. Ho trovato **{len(new_lessons)}** didattiche sincrone programmate."
        notify("✅ Mercatorum monitor attivato", body)
    else:
        added, removed, modified = diff_lessons(old_lessons, new_lessons)
        if added or removed or modified:
            notify("📚 Modifica didattiche sincrone Mercatorum", build_change_message(added, removed, modified))

    calendar_events = sync_calendar(
        old_lessons,
        new_lessons,
        calendar_events,
        added,
        removed,
        modified,
    )

    reminder_lines: list[str] = []
    for lesson in new_lessons:
        minutes = (lesson.start_dt - now).total_seconds() / 60
        if 0 <= minutes <= REMINDER_MINUTES and lesson.reminder_key not in reminded:
            reminder_lines.append(f"- Tra circa {max(1, round(minutes))} min: {fmt_lesson(lesson)}")
            reminded.add(lesson.reminder_key)

    if reminder_lines:
        notify("⏰ Didattica sincrona in arrivo", "\n".join(reminder_lines))

    active_reminder_keys = {lesson.reminder_key for lesson in new_lessons}
    reminded.intersection_update(active_reminder_keys)
    save_state(new_lessons, reminded, calendar_events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
