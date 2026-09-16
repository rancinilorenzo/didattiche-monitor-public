from __future__ import annotations

import hashlib
import json
import re

import monitor_runner_v3 as v3
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def norm(value: str) -> str:
    return (
        v3.core.normalize_space(value)
        .casefold()
        .replace("’", "'")
        .replace("`", "'")
    )


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
                raise RuntimeError("Login ancora visibile.")

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
                raise RuntimeError("Scheda Terminate non trovata.")

            tab.click(timeout=10_000)
            v3.core.settle_spa(page, 2000)

            try:
                page.wait_for_function(
                    r"""
                    () => /accedi\s+al\s+test/i.test(
                      document.body.innerText || ''
                    )
                    """,
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                pass

            body_text = page.locator("body").inner_text(timeout=15_000)
            text = v3.core.normalize_space(body_text)
            date_matches = list(v3.core.DATE_RE.finditer(text))

            rows = []
            stages = {
                "date_time_rows": 0,
                "subject_at_start": 0,
                "subject_later": 0,
                "no_official_subject_found": 0,
                "presence_after_start_subject": 0,
                "test_column_after_start_subject": 0,
                "nonempty_title_after_start_subject": 0,
            }

            for index, match in enumerate(date_matches):
                block_end = (
                    date_matches[index + 1].start()
                    if index + 1 < len(date_matches)
                    else len(text)
                )
                block = text[match.start():block_end]
                time_match = v3.core.TIME_RE.search(block)
                if time_match is None:
                    continue

                stages["date_time_rows"] += 1
                tail = v3.core.normalize_space(block[time_match.end():])
                tail_norm = norm(tail)

                split = v3.split_official_subject(tail)
                if split is not None:
                    stages["subject_at_start"] += 1
                    subject, remainder = split
                    presence = re.search(
                        r"%\s*presenza\b",
                        remainder,
                        flags=re.IGNORECASE,
                    )
                    if presence is not None:
                        stages["presence_after_start_subject"] += 1
                        before_presence = remainder[:presence.start()]
                        tests = list(
                            re.finditer(
                                r"\bTest\b",
                                before_presence,
                                flags=re.IGNORECASE,
                            )
                        )
                        if tests:
                            stages["test_column_after_start_subject"] += 1
                            title = v3.core.normalize_space(
                                before_presence[:tests[-1].start()]
                            )
                            updating = re.search(
                                r"\bIn\s+aggiornamento\b",
                                title,
                                flags=re.IGNORECASE,
                            )
                            if updating is not None:
                                title = v3.core.normalize_space(
                                    title[:updating.start()]
                                )
                            if title:
                                stages["nonempty_title_after_start_subject"] += 1

                    rows.append({
                        "row": len(rows),
                        "subject_position": "start",
                        "prefix_words": 0,
                        "prefix_group": None,
                    })
                    continue

                earliest = None
                for subject in v3.OFFICIAL_MERCATORUM_SUBJECTS:
                    subject_norm = norm(subject)
                    pos = tail_norm.find(subject_norm)
                    if pos < 0:
                        continue
                    if earliest is None or pos < earliest:
                        earliest = pos

                if earliest is None:
                    stages["no_official_subject_found"] += 1
                    rows.append({
                        "row": len(rows),
                        "subject_position": "not_found",
                        "prefix_words": None,
                        "prefix_group": None,
                    })
                    continue

                stages["subject_later"] += 1
                prefix = tail_norm[:earliest].strip()
                prefix_words = len(prefix.split()) if prefix else 0
                prefix_group = (
                    hashlib.sha1(prefix.encode("utf-8"))
                    .hexdigest()[:8]
                    if prefix
                    else None
                )
                rows.append({
                    "row": len(rows),
                    "subject_position": "later",
                    "prefix_words": prefix_words,
                    "prefix_group": prefix_group,
                })

            result = {
                "stages": stages,
                "rows": rows,
            }

            print("=== TERMINATE PROBE V3.5H ===")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("=== FINE TERMINATE PROBE ===")

            if stages["date_time_rows"] <= 0:
                raise SystemExit(1)

            v3.core.best_effort_logout(page)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()