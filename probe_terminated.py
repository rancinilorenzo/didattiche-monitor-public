from __future__ import annotations

import json
import re

import monitor_runner_v3 as v3
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def main() -> None:
    with v3.legacy.sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
        )
        context = browser.new_context(
            locale="it-IT",
            timezone_id=v3.core.TIMEZONE_NAME,
        )
        page = context.new_page()

        try:
            page.goto(
                v3.core.SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            v3.core.settle_spa(page, 2000)
            v3.core.login_if_needed(page)

            page.goto(
                v3.core.SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            v3.core.settle_spa(page, 2000)

            if v3.core.first_visible(
                page,
                ["#password", "input[type='password']"],
            ):
                v3.core.login_if_needed(page)
                page.goto(
                    v3.core.SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                v3.core.settle_spa(page, 2000)

            if v3.core.first_visible(
                page,
                ["#password", "input[type='password']"],
            ):
                raise RuntimeError(
                    "Mercatorum mostra ancora la pagina di login."
                )

            tab = v3.core.first_visible(
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
            v3.core.settle_spa(page, 2000)

            try:
                page.wait_for_function(
                    r"""
                    () =>
                      /accedi\s+al\s+test/i.test(
                        document.body.innerText || ''
                      )
                    """,
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                pass

            body_text = page.locator(
                "body"
            ).inner_text(timeout=15_000)

            text = v3.core.normalize_space(body_text)
            date_matches = list(
                v3.core.DATE_RE.finditer(text)
            )

            rows = []
            summary = {
                "date_time_rows": 0,
                "subject_at_start": 0,
                "presence_found": 0,
                "recording_found": 0,
                "access_found": 0,
                "test_before_presence_rows": 0,
                "test_between_presence_and_recording_rows": 0,
                "test_between_recording_and_access_rows": 0,
                "pre_presence_ends_with_test_rows": 0,
                "candidate_nonempty_after_safe_suffix_strip": 0,
            }

            for index, match in enumerate(date_matches):
                end = (
                    date_matches[index + 1].start()
                    if index + 1 < len(date_matches)
                    else len(text)
                )
                block = text[match.start():end]
                tm = v3.core.TIME_RE.search(block)

                if tm is None:
                    continue

                summary["date_time_rows"] += 1

                tail = v3.core.normalize_space(
                    block[tm.end():]
                )
                split = v3.split_official_subject(tail)

                row = {
                    "row": len(rows),
                    "subject_at_start": split is not None,
                    "presence": False,
                    "recording": False,
                    "access": False,
                    "test_before_presence": 0,
                    "test_between_presence_and_recording": 0,
                    "test_between_recording_and_access": 0,
                    "pre_presence_ends_with_test": False,
                    "candidate_nonempty_after_safe_suffix_strip": False,
                }

                if split is None:
                    rows.append(row)
                    continue

                summary["subject_at_start"] += 1
                _, remainder = split

                presence = re.search(
                    r"%\s*presenza\b",
                    remainder,
                    flags=re.IGNORECASE,
                )
                recording = re.search(
                    r"\bregistrazione\b",
                    remainder,
                    flags=re.IGNORECASE,
                )
                access = re.search(
                    r"\baccedi\s+al\s+test\b",
                    remainder,
                    flags=re.IGNORECASE,
                )
                tests = list(
                    re.finditer(
                        r"\btest\b",
                        remainder,
                        flags=re.IGNORECASE,
                    )
                )

                if presence is not None:
                    row["presence"] = True
                    summary["presence_found"] += 1

                if recording is not None:
                    row["recording"] = True
                    summary["recording_found"] += 1

                if access is not None:
                    row["access"] = True
                    summary["access_found"] += 1

                if presence is not None:
                    before_presence = [
                        t for t in tests
                        if t.start() < presence.start()
                    ]
                    row["test_before_presence"] = len(
                        before_presence
                    )

                    if before_presence:
                        summary[
                            "test_before_presence_rows"
                        ] += 1

                    pre = v3.core.normalize_space(
                        remainder[:presence.start()]
                    )

                    row[
                        "pre_presence_ends_with_test"
                    ] = bool(
                        re.search(
                            r"\btest\b\s*$",
                            pre,
                            flags=re.IGNORECASE,
                        )
                    )

                    if row["pre_presence_ends_with_test"]:
                        summary[
                            "pre_presence_ends_with_test_rows"
                        ] += 1

                    candidate = pre

                    candidate = re.sub(
                        r"\bIn\s+aggiornamento\b\s*$",
                        "",
                        candidate,
                        flags=re.IGNORECASE,
                    )
                    candidate = v3.core.normalize_space(candidate)

                    candidate = re.sub(
                        r"\bTest\b\s*$",
                        "",
                        candidate,
                        flags=re.IGNORECASE,
                    )
                    candidate = v3.core.normalize_space(candidate)

                    candidate = re.sub(
                        r"\bIn\s+aggiornamento\b\s*$",
                        "",
                        candidate,
                        flags=re.IGNORECASE,
                    )
                    candidate = v3.core.normalize_space(candidate)

                    row[
                        "candidate_nonempty_after_safe_suffix_strip"
                    ] = bool(candidate)

                    if candidate:
                        summary[
                            "candidate_nonempty_after_safe_suffix_strip"
                        ] += 1

                if (
                    presence is not None
                    and recording is not None
                    and recording.start() > presence.start()
                ):
                    between = [
                        t for t in tests
                        if (
                            presence.end()
                            <= t.start()
                            < recording.start()
                        )
                    ]
                    row[
                        "test_between_presence_and_recording"
                    ] = len(between)

                    if between:
                        summary[
                            "test_between_presence_and_recording_rows"
                        ] += 1

                if (
                    recording is not None
                    and access is not None
                    and access.start() > recording.start()
                ):
                    between = [
                        t for t in tests
                        if (
                            recording.end()
                            <= t.start()
                            < access.start()
                        )
                    ]
                    row[
                        "test_between_recording_and_access"
                    ] = len(between)

                    if between:
                        summary[
                            "test_between_recording_and_access_rows"
                        ] += 1

                rows.append(row)

            result = {
                "summary": summary,
                "rows": rows,
            }

            print("=== TERMINATE PROBE V3.5I ===")
            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print("=== FINE TERMINATE PROBE ===")

            green = all(
                [
                    summary["date_time_rows"] > 0,
                    summary["subject_at_start"]
                    == summary["date_time_rows"],
                    summary["presence_found"]
                    == summary["date_time_rows"],
                    summary["recording_found"]
                    == summary["date_time_rows"],
                    summary["access_found"]
                    == summary["date_time_rows"],
                    summary[
                        "candidate_nonempty_after_safe_suffix_strip"
                    ] == summary["date_time_rows"],
                ]
            )

            if not green:
                raise SystemExit(1)

            v3.core.best_effort_logout(page)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()