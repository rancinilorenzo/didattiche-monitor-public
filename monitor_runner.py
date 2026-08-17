from __future__ import annotations

import hashlib
import os
from datetime import datetime

import monitor as core


def parse_reminder_minutes() -> list[int]:
    raw = os.getenv("REMINDER_MINUTES", "45,5")
    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError(
                "REMINDER_MINUTES deve contenere minuti interi "
                "separati da virgola, es. 45,5"
            ) from exc

        if value > 0:
            values.add(value)

    if not values:
        raise RuntimeError(
            "REMINDER_MINUTES non contiene alcun valore valido."
        )

    return sorted(values, reverse=True)


REMINDER_MINUTES = parse_reminder_minutes()


def reminder_state_key(
    lesson: core.Lesson,
    threshold: int,
) -> str:
    value = (
        f"{lesson.exact_key}|telegram-reminder|{threshold}"
    )
    return hashlib.sha1(
        value.encode("utf-8")
    ).hexdigest()[:16]


def fmt_date(value: str) -> str:
    return datetime.fromisoformat(
        value
    ).strftime("%d/%m/%Y")


def lesson_name(lesson: core.Lesson) -> str:
    return (
        core.normalize_space(lesson.description)
        or "Didattica sincrona"
    )


def lesson_card(lesson: core.Lesson) -> str:
    return (
        f"{lesson_name(lesson)}\n"
        f"📅 {fmt_date(lesson.date)}\n"
        f"🕒 {lesson.start}–{lesson.end}"
    )


def modified_card(
    before: core.Lesson,
    after: core.Lesson,
) -> str:

    date_changed = before.date != after.date

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

    if date_changed and time_changed:
        header = (
            "📅🕒 GIORNO E ORARIO MODIFICATI"
        )

    elif date_changed:
        header = "📅 GIORNO MODIFICATO"

    elif time_changed:
        header = "🕒 ORARIO MODIFICATO"

    else:
        header = "✏️ DIDATTICA MODIFICATA"

    lines = [
        header,
        "",
        lesson_name(after),
    ]

    if date_changed or time_changed:
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
        tuple[core.Lesson, core.Lesson]
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
            modified_card(before, after)
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
        f"⏳ Inizia tra circa {rounded} min"
    )


def main() -> int:

    state = core.load_state()

    old_lessons = [
        core.lesson_from_dict(x)
        for x in state.get("events", [])
    ]

    reminded = set(
        state.get("reminded", [])
    )

    calendar_events = dict(
        state.get("calendar_events", {})
    )

    new_lessons = core.scrape_lessons()

    now = datetime.now(core.TIMEZONE)

    added: list[core.Lesson] = []
    removed: list[core.Lesson] = []

    modified: list[
        tuple[core.Lesson, core.Lesson]
    ] = []

    if not old_lessons:

        core.notify(
            "✅ Mercatorum monitor attivato",
            (
                "Monitor attivato. "
                f"Ho trovato {len(new_lessons)} "
                "didattiche sincrone programmate."
            ),
        )

    else:

        added, removed, modified = (
            core.diff_lessons(
                old_lessons,
                new_lessons,
            )
        )

        if added or removed or modified:

            core.notify(
                "📚 Aggiornamento Mercatorum",
                build_change_message(
                    added,
                    removed,
                    modified,
                ),
            )

    calendar_events = core.sync_calendar(
        old_lessons,
        new_lessons,
        calendar_events,
        added,
        removed,
        modified,
    )

    # Migrazione dal vecchio reminder
    # singolo a quello 45 + 5.
    old_exact_keys = {
        lesson.exact_key
        for lesson in old_lessons
    }

    farthest_threshold = max(
        REMINDER_MINUTES
    )

    for lesson in new_lessons:

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

    reminder_groups: dict[
        int,
        list[str],
    ] = {}

    for lesson in new_lessons:

        minutes = (
            lesson.start_dt - now
        ).total_seconds() / 60

        if minutes < 0:
            continue

        due_thresholds = [
            threshold
            for threshold
            in REMINDER_MINUTES
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

        # Se il monitor vede per la prima
        # volta una lezione quando mancano
        # meno di 5 minuti, evita di
        # mandare sia 45 che 5 insieme.
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
                "⏰ DIDATTICA IN ARRIVO"
                f" · {threshold} MIN"
            ),
            "\n\n".join(
                reminder_groups[threshold]
            ),
        )

    active_reminder_keys = {
        reminder_state_key(
            lesson,
            threshold,
        )
        for lesson in new_lessons
        for threshold
        in REMINDER_MINUTES
    }

    reminded.intersection_update(
        active_reminder_keys
    )

    core.save_state(
        new_lessons,
        reminded,
        calendar_events,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
