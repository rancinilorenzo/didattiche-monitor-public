from __future__ import annotations

import hashlib
import os
from datetime import datetime

from playwright.sync_api import sync_playwright

import monitor as core


# ============================================================
# CONFIGURAZIONE
# ============================================================

EMPTY_LIVE_TEXT = "nessuna lezione in diretta"

# Se una lezione sparisce da "Programmate"
# molto vicino all'orario previsto e nello stesso
# momento Mercatorum segnala una diretta, non la
# consideriamo cancellata.
LIVE_TRANSITION_GRACE_MINUTES = 10

# Chiave salvata nello state.enc per ricordare
# che la notifica "in diretta" è già stata inviata.
LIVE_STATE_KEY = hashlib.sha1(
    b"mercatorum-live-present"
).hexdigest()[:16]


# ============================================================
# REMINDER TELEGRAM
# ============================================================

def parse_telegram_reminder_minutes() -> list[int]:
    raw = os.getenv(
        "TELEGRAM_REMINDER_MINUTES",
        "45,5",
    )

    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip()

        if not part:
            continue

        try:
            value = int(part)

        except ValueError as exc:
            raise RuntimeError(
                "TELEGRAM_REMINDER_MINUTES deve contenere "
                "minuti interi separati da virgola, es. 45,5"
            ) from exc

        if value > 0:
            values.add(value)

    if not values:
        raise RuntimeError(
            "TELEGRAM_REMINDER_MINUTES non contiene "
            "alcun valore valido."
        )

    return sorted(
        values,
        reverse=True,
    )


TELEGRAM_REMINDER_MINUTES = (
    parse_telegram_reminder_minutes()
)


def reminder_state_key(
    lesson: core.Lesson,
    threshold: int,
) -> str:

    value = (
        f"{lesson.exact_key}"
        f"|telegram-reminder|{threshold}"
    )

    return hashlib.sha1(
        value.encode("utf-8")
    ).hexdigest()[:16]


# ============================================================
# FORMATTAZIONE
# ============================================================

def fmt_date(
    value: str,
) -> str:

    return datetime.fromisoformat(
        value
    ).strftime("%d/%m/%Y")


def lesson_name(
    lesson: core.Lesson,
) -> str:

    return (
        core.normalize_space(
            lesson.description
        )
        or "Didattica sincrona"
    )


def lesson_card(
    lesson: core.Lesson,
) -> str:

    return (
        f"{lesson_name(lesson)}\n"
        f"📅 {fmt_date(lesson.date)}\n"
        f"🕒 {lesson.start}–{lesson.end}"
    )


def modified_card(
    before: core.Lesson,
    after: core.Lesson,
) -> str:

    date_changed = (
        before.date != after.date
    )

    time_changed = (
        before.start,
        before.end,
    ) != (
        after.start,
        after.end,
    )

    name_changed = (
        lesson_name(before)
        != lesson_name(after)
    )

    if (
        date_changed
        and time_changed
    ):
        header = (
            "📅🕒 GIORNO E ORARIO MODIFICATI"
        )

    elif date_changed:
        header = (
            "📅 GIORNO MODIFICATO"
        )

    elif time_changed:
        header = (
            "🕒 ORARIO MODIFICATO"
        )

    else:
        header = (
            "✏️ DIDATTICA MODIFICATA"
        )

    lines = [
        header,
        "",
        lesson_name(after),
    ]

    if (
        date_changed
        or time_changed
    ):
        lines.extend(
            [
                "",
                (
                    f"Prima: "
                    f"{fmt_date(before.date)} · "
                    f"{before.start}–{before.end}"
                ),
                (
                    f"Ora:   "
                    f"{fmt_date(after.date)} · "
                    f"{after.start}–{after.end}"
                ),
            ]
        )

    if name_changed:
        lines.extend(
            [
                "",
                (
                    "Titolo prima: "
                    f"{lesson_name(before)}"
                ),
                (
                    "Titolo ora:   "
                    f"{lesson_name(after)}"
                ),
            ]
        )

    return "\n".join(lines)


def build_change_message(
    added: list[core.Lesson],
    removed: list[core.Lesson],
    modified: list[
        tuple[
            core.Lesson,
            core.Lesson,
        ]
    ],
) -> str:

    blocks: list[str] = []

    for lesson in added:
        blocks.append(
            "🆕 DIDATTICA AGGIUNTA\n\n"
            + lesson_card(lesson)
        )

    for before, after in modified:
        blocks.append(
            modified_card(
                before,
                after,
            )
        )

    for lesson in removed:
        blocks.append(
            "🗑️ DIDATTICA RIMOSSA\n\n"
            + lesson_card(lesson)
        )

    return (
        "\n\n────────────\n\n"
    ).join(blocks)


def reminder_card(
    lesson: core.Lesson,
    actual_minutes: float,
) -> str:

    rounded = max(
        1,
        round(actual_minutes),
    )

    return (
        f"{lesson_card(lesson)}\n"
        f"⏳ Inizia tra circa "
        f"{rounded} min"
    )


# ============================================================
# CONTROLLO SCHEDA "IN CORSO"
# ============================================================

def click_live_tab(
    page,
) -> None:

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
        raise RuntimeError(
            "Non trovo la scheda 'In corso'."
        )

    tab.click(
        timeout=10_000
    )

    core.settle_spa(
        page,
        1500,
    )


def page_live_status(
    page,
) -> bool:

    text = core.normalize_space(
        page.locator(
            "body"
        ).inner_text(
            timeout=15_000
        )
    ).lower()

    if (
        "didattica sincrona"
        not in text
    ):
        raise RuntimeError(
            "La pagina 'In corso' "
            "non sembra caricata "
            "correttamente."
        )

    if (
        EMPTY_LIVE_TEXT
        in text
    ):
        return False

    return True


def detect_live_present(
    page,
) -> bool:

    click_live_tab(
        page
    )

    first_result = (
        page_live_status(
            page
        )
    )

    if not first_result:
        return False

    # Secondo controllo dopo 3 secondi.
    # Evita di interpretare il caricamento
    # della SPA come una vera diretta.
    page.wait_for_timeout(
        3000
    )

    return page_live_status(
        page
    )


# ============================================================
# LOGIN + LETTURA MERCATORUM
# ============================================================

def scrape_snapshot() -> tuple[
    list[core.Lesson],
    bool | None,
]:

    """
    Con una sola sessione Mercatorum:

    1. legge le didattiche Programmate;
    2. controlla la scheda In corso.

    Se il controllo In corso fallisce,
    il monitor principale continua comunque.
    """

    core.DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
        )

        context = browser.new_context(
            locale="it-IT",
            timezone_id=(
                core.TIMEZONE_NAME
            ),
        )

        page = context.new_page()

        try:

            # --------------------------------
            # APERTURA / LOGIN
            # --------------------------------

            page.goto(
                core.SCHEDULE_URL,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=45_000,
            )

            core.settle_spa(
                page,
                2000,
            )

            core.login_if_needed(
                page
            )

            # Dopo il login torniamo
            # esplicitamente alla pagina
            # delle didattiche Programmate.
            page.goto(
                core.SCHEDULE_URL,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=45_000,
            )

            core.settle_spa(
                page,
                2000,
            )

            # Secondo tentativo se la SPA
            # ha mostrato il login solo dopo.
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
                    wait_until=(
                        "domcontentloaded"
                    ),
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
                    "Mercatorum mostra ancora "
                    "la pagina di login."
                )

            # --------------------------------
            # PROGRAMMATE
            # --------------------------------

            body_text = page.locator(
                "body"
            ).inner_text(
                timeout=15_000
            )

            lessons = core.parse_lessons(
                body_text
            )

            if not lessons:

                # Non assumiamo che zero
                # risultati significhi davvero
                # zero lezioni: potrebbe essere
                # cambiata la pagina.
                try:

                    page.screenshot(
                        path=str(
                            core.DEBUG_DIR
                            / "programmate-empty.png"
                        ),
                        full_page=True,
                    )

                    (
                        core.DEBUG_DIR
                        / "programmate-empty.txt"
                    ).write_text(
                        body_text,
                        encoding="utf-8",
                    )

                except Exception:
                    pass

                raise RuntimeError(
                    "Nessuna didattica "
                    "programmata riconosciuta. "
                    "Non aggiorno lo stato per "
                    "evitare false cancellazioni."
                )

            # --------------------------------
            # IN CORSO
            # --------------------------------

            live_present: (
                bool | None
            )

            try:

                live_present = (
                    detect_live_present(
                        page
                    )
                )

            except Exception as exc:

                # Il controllo live non deve
                # bloccare reminder, calendario
                # o controllo Programmate.
                print(
                    "Controllo 'In corso' "
                    "non riuscito: "
                    f"{type(exc).__name__}"
                )

                live_present = None

            core.best_effort_logout(
                page
            )

            return (
                lessons,
                live_present,
            )

        except Exception:

            try:

                page.screenshot(
                    path=str(
                        core.DEBUG_DIR
                        / "failure.png"
                    ),
                    full_page=True,
                )

            except Exception:
                pass

            raise

        finally:

            context.close()
            browser.close()


# ============================================================
# PROGRAMMATE -> IN CORSO
# ============================================================

def filter_natural_live_transitions(
    removed: list[core.Lesson],
    now: datetime,
    live_present: bool | None,
) -> tuple[
    list[core.Lesson],
    list[core.Lesson],
]:

    """
    Restituisce:

    - vere rimozioni;
    - lezioni probabilmente passate
      da Programmate a In corso.
    """

    real_removed: list[
        core.Lesson
    ] = []

    transitions: list[
        core.Lesson
    ] = []

    for lesson in removed:

        # Se l'orario è già iniziato,
        # la sparizione da Programmate
        # non deve essere interpretata
        # come cancellazione.
        if (
            lesson.start_dt
            <= now
        ):

            transitions.append(
                lesson
            )

            continue

        minutes_to_start = (
            lesson.start_dt - now
        ).total_seconds() / 60

        # Diretta confermata e lezione
        # vicina all'orario previsto.
        if (
            live_present is True
            and minutes_to_start
            <= LIVE_TRANSITION_GRACE_MINUTES
        ):

            transitions.append(
                lesson
            )

            continue

        # Se il controllo In corso ha avuto
        # un errore temporaneo, per pochi
        # minuti evitiamo una falsa
        # cancellazione.
        if (
            live_present is None
            and minutes_to_start
            <= LIVE_TRANSITION_GRACE_MINUTES
        ):

            transitions.append(
                lesson
            )

            continue

        real_removed.append(
            lesson
        )

    return (
        real_removed,
        transitions,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    # --------------------------------
    # STATO PRECEDENTE
    # --------------------------------

    state = core.load_state()

    old_lessons = [
        core.lesson_from_dict(x)
        for x in state.get(
            "events",
            [],
        )
    ]

    reminded = set(
        state.get(
            "reminded",
            [],
        )
    )

    calendar_events = dict(
        state.get(
            "calendar_events",
            {},
        )
    )

    # --------------------------------
    # NUOVO SNAPSHOT
    # --------------------------------

    (
        new_lessons,
        live_present,
    ) = scrape_snapshot()

    now = datetime.now(
        core.TIMEZONE
    )

    added: list[
        core.Lesson
    ] = []

    removed: list[
        core.Lesson
    ] = []

    modified: list[
        tuple[
            core.Lesson,
            core.Lesson,
        ]
    ] = []

    transitions: list[
        core.Lesson
    ] = []

    # ========================================================
    # AGGIUNTE / MODIFICHE / RIMOZIONI
    # ========================================================

    if not old_lessons:

        core.notify(
            (
                "✅ Mercatorum "
                "monitor attivato"
            ),
            (
                "Monitor attivato. "
                f"Ho trovato "
                f"{len(new_lessons)} "
                "didattiche sincrone "
                "programmate."
            ),
        )

    else:

        # Le lezioni già iniziate non devono
        # risultare cancellate solo perché
        # Mercatorum le sposta in "In corso".
        old_for_diff = [
            lesson
            for lesson
            in old_lessons
            if lesson.start_dt > now
        ]

        new_for_diff = [
            lesson
            for lesson
            in new_lessons
            if lesson.start_dt > now
        ]

        (
            added,
            raw_removed,
            modified,
        ) = core.diff_lessons(
            old_for_diff,
            new_for_diff,
        )

        (
            removed,
            transitions,
        ) = filter_natural_live_transitions(
            raw_removed,
            now,
            live_present,
        )

        if (
            added
            or removed
            or modified
        ):

            core.notify(
                (
                    "📚 Aggiornamento "
                    "Mercatorum"
                ),
                build_change_message(
                    added,
                    removed,
                    modified,
                ),
            )

    # ========================================================
    # GOOGLE CALENDAR
    # ========================================================

    # Se una lezione è appena passata a
    # "In corso", la teniamo temporaneamente
    # nello snapshot Calendar per non perdere
    # il collegamento con l'evento già creato.
    calendar_lessons = list(
        new_lessons
    )

    known_calendar_keys = {
        lesson.exact_key
        for lesson
        in calendar_lessons
    }

    for lesson in transitions:

        if (
            lesson.exact_key
            not in known_calendar_keys
        ):

            calendar_lessons.append(
                lesson
            )

            known_calendar_keys.add(
                lesson.exact_key
            )

    calendar_events = (
        core.sync_calendar(
            old_lessons,
            calendar_lessons,
            calendar_events,
            added,
            removed,
            modified,
        )
    )

    # ========================================================
    # MIGRAZIONE VECCHIO REMINDER SINGOLO
    # ========================================================

    old_exact_keys = {
        lesson.exact_key
        for lesson
        in old_lessons
    }

    farthest_threshold = max(
        TELEGRAM_REMINDER_MINUTES
    )

    for lesson in new_lessons:

        # La vecchia versione di monitor.py
        # usava lesson.reminder_key.
        # Se il reminder da 45 era già partito,
        # lo trasformiamo nel nuovo formato.
        if (
            lesson.exact_key
            in old_exact_keys
            and lesson.reminder_key
            in reminded
        ):

            reminded.add(
                reminder_state_key(
                    lesson,
                    farthest_threshold,
                )
            )

    # ========================================================
    # REMINDER TELEGRAM 45 + 5 MINUTI
    # ========================================================

    reminder_groups: dict[
        int,
        list[str],
    ] = {}

    for lesson in new_lessons:

        minutes = (
            lesson.start_dt - now
        ).total_seconds() / 60

        if minutes <= 0:
            continue

        due_thresholds = [
            threshold
            for threshold
            in TELEGRAM_REMINDER_MINUTES
            if (
                minutes <= threshold
                and reminder_state_key(
                    lesson,
                    threshold,
                )
                not in reminded
            )
        ]

        if not due_thresholds:
            continue

        # Se una nuova didattica compare
        # quando mancano già meno di 5 minuti,
        # mandiamo solo il reminder più vicino.
        chosen_threshold = min(
            due_thresholds
        )

        reminder_groups.setdefault(
            chosen_threshold,
            [],
        ).append(
            reminder_card(
                lesson,
                minutes,
            )
        )

        # Tutte le soglie ormai superate
        # vengono marcate come gestite.
        for threshold in due_thresholds:

            reminded.add(
                reminder_state_key(
                    lesson,
                    threshold,
                )
            )

    for threshold in sorted(
        reminder_groups,
        reverse=True,
    ):

        core.notify(
            (
                "⏰ DIDATTICA "
                "IN ARRIVO"
                f" · {threshold} MIN"
            ),
            "\n\n".join(
                reminder_groups[
                    threshold
                ]
            ),
        )

    # ========================================================
    # NOTIFICA REALE "IN CORSO"
    # ========================================================

    if live_present is True:

        if (
            LIVE_STATE_KEY
            not in reminded
        ):

            core.notify(
                (
                    "🔴 DIDATTICA "
                    "IN DIRETTA"
                ),
                (
                    "Mercatorum segnala "
                    "una didattica nello "
                    "stato \"In corso\".\n\n"
                    "▶️ È disponibile "
                    "adesso sulla piattaforma."
                ),
            )

            reminded.add(
                LIVE_STATE_KEY
            )

    elif live_present is False:

        # Quando torna:
        # "Nessuna lezione in diretta"
        # riarmiamo la notifica.
        reminded.discard(
            LIVE_STATE_KEY
        )

    # Se live_present è None:
    # nessuna modifica al flag live,
    # evitando notifiche duplicate.

    # ========================================================
    # PULIZIA STATO REMINDER
    # ========================================================

    active_state_keys = {
        reminder_state_key(
            lesson,
            threshold,
        )
        for lesson
        in new_lessons
        for threshold
        in TELEGRAM_REMINDER_MINUTES
    }

    if (
        LIVE_STATE_KEY
        in reminded
    ):
        active_state_keys.add(
            LIVE_STATE_KEY
        )

    reminded.intersection_update(
        active_state_keys
    )

    # ========================================================
    # STATO DA SALVARE
    # ========================================================

    state_lessons = list(
        new_lessons
    )

    known_state_keys = {
        lesson.exact_key
        for lesson
        in state_lessons
    }

    # Manteniamo temporaneamente una lezione
    # che sta transitando a "In corso".
    # Evita falsi cambiamenti e duplicati
    # Calendar durante la transizione.
    for lesson in transitions:

        if (
            lesson.exact_key
            not in known_state_keys
        ):

            state_lessons.append(
                lesson
            )

            known_state_keys.add(
                lesson.exact_key
            )

    # ========================================================
    # SALVATAGGIO CIFRATO
    # ========================================================

    core.save_state(
        state_lessons,
        reminded,
        calendar_events,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
