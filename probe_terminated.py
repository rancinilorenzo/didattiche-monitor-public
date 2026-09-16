from __future__ import annotations

import json

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
                [
                    "#password",
                    "input[type='password']",
                ],
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
                [
                    "#password",
                    "input[type='password']",
                ],
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

            accedi_seen = False

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
                accedi_seen = True
            except PlaywrightTimeoutError:
                pass

            body_text = page.locator(
                "body"
            ).inner_text(timeout=15_000)

            blocks = v3.terminated_blocks_v35(
                body_text
            )
            controls = v3.terminated_test_controls_v35(
                page
            )

            parsed = 0
            required_signals = 0
            updating = 0
            active = 0
            identities = []

            for block in blocks:
                lesson = block.get("lesson")
                meta = block.get("meta")

                if (
                    isinstance(lesson, v3.core.Lesson)
                    and isinstance(meta, dict)
                ):
                    identity = v3.strict_identity_from_meta(
                        lesson,
                        meta,
                    )

                    if identity is not None:
                        parsed += 1
                        identities.append(
                            (
                                lesson.date,
                                lesson.start,
                                lesson.end,
                                identity[0],
                                identity[1],
                            )
                        )

                if all(
                    [
                        block.get("contains_accedi_al_test"),
                        block.get("contains_test_label"),
                        block.get("contains_presence"),
                        block.get("contains_recording"),
                    ]
                ):
                    required_signals += 1

                if block.get("contains_in_aggiornamento"):
                    updating += 1

            for control in controls:
                if control.get("active") is True:
                    active += 1

            result = {
                "accedi_al_test_seen_after_wait": accedi_seen,
                "terminated_blocks": len(blocks),
                "test_controls": len(controls),
                "counts_match": (
                    len(blocks) > 0
                    and len(blocks) == len(controls)
                ),
                "parsed_identities": parsed,
                "all_rows_parsed": (
                    len(blocks) > 0
                    and parsed == len(blocks)
                ),
                "rows_with_required_signals": required_signals,
                "all_required_signals_present": (
                    len(blocks) > 0
                    and required_signals == len(blocks)
                ),
                "unique_identity_schedules": len(set(identities)),
                "all_identity_schedules_unique": (
                    len(blocks) > 0
                    and len(set(identities)) == len(blocks)
                ),
                "active_test_controls": active,
                "updating_rows": updating,
            }

            print("=== TERMINATE PROBE V3.5G ===")
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
                    result["accedi_al_test_seen_after_wait"],
                    result["counts_match"],
                    result["all_rows_parsed"],
                    result["all_required_signals_present"],
                    result["all_identity_schedules_unique"],
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