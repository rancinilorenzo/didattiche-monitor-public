from __future__ import annotations

import json
import re

import monitor as core
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


def terminated_blocks(body_text: str) -> list[dict]:
    text = core.normalize_space(
        body_text
    )

    matches = list(
        core.DATE_RE.finditer(
            text
        )
    )

    result: list[dict] = []

    for index, match in enumerate(
        matches
    ):
        block_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        block = text[
            match.start():block_end
        ]

        time_match = (
            core.TIME_RE.search(
                block
            )
        )

        if time_match is None:
            continue

        low = block.casefold()

        result.append(
            {
                "index": len(result),

                "contains_in_aggiornamento":
                    "in aggiornamento"
                    in low,

                "contains_accedi_al_test":
                    "accedi al test"
                    in low,

                "contains_test_label":
                    bool(
                        re.search(
                            r"\btest\b",
                            block,
                            re.IGNORECASE,
                        )
                    ),

                "contains_presence":
                    bool(
                        re.search(
                            r"%\s*presenza",
                            block,
                            re.IGNORECASE,
                        )
                    ),

                "contains_recording":
                    "registrazione"
                    in low,
            }
        )

    return result


def main():
    with sync_playwright() as p:
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
                    "Mercatorum mostra ancora il login."
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

            tab.click(
                timeout=10_000
            )

            core.settle_spa(
                page,
                2000,
            )

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

            body_text = (
                page.locator("body")
                .inner_text(
                    timeout=15_000
                )
            )

            blocks = terminated_blocks(
                body_text
            )

            controls = page.evaluate(
                r"""
                () => {
                  const norm = value =>
                    (value || '')
                    .replace(/\s+/g, ' ')
                    .trim();

                  const all =
                    Array.from(
                      document.querySelectorAll(
                        'body *'
                      )
                    );

                  const leaves =
                    all.filter(el => {
                      const text =
                        norm(
                          el.innerText ||
                          el.textContent
                        );

                      if (
                        !/^accedi\s+al\s+test$/i
                        .test(text)
                      ) {
                        return false;
                      }

                      const childHasSameText =
                        Array.from(
                          el.children
                        ).some(child =>
                          /^accedi\s+al\s+test$/i
                          .test(
                            norm(
                              child.innerText ||
                              child.textContent
                            )
                          )
                        );

                      if (childHasSameText) {
                        return false;
                      }

                      const style =
                        window.getComputedStyle(
                          el
                        );

                      return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden'
                      );
                    });

                  return leaves.map(
                    (leaf, index) => {
                      const control =
                        leaf.closest(
                          [
                            'button',
                            'a',
                            '[role="button"]',
                            '[tabindex]'
                          ].join(',')
                        ) || leaf;

                      const style =
                        window.getComputedStyle(
                          control
                        );

                      const ariaDisabled =
                        control.getAttribute(
                          'aria-disabled'
                        );

                      const disabled =
                        control.disabled === true ||
                        control.matches(
                          ':disabled'
                        ) ||
                        control.hasAttribute(
                          'disabled'
                        ) ||
                        ariaDisabled === 'true' ||
                        style.pointerEvents ===
                          'none';

                      const href =
                        control.getAttribute(
                          'href'
                        );

                      const validHref =
                        Boolean(href) &&
                        href !== '#' &&
                        href !== '' &&
                        !href
                          .toLowerCase()
                          .startsWith(
                            'javascript:'
                          );

                      let active = false;

                      if (
                        control.tagName ===
                        'BUTTON'
                      ) {
                        active =
                          !disabled;
                      }

                      if (
                        control.tagName ===
                        'A'
                      ) {
                        active =
                          !disabled &&
                          validHref;
                      }

                      if (
                        control.tagName !==
                          'BUTTON' &&
                        control.tagName !==
                          'A'
                      ) {
                        active =
                          !disabled &&
                          (
                            control.getAttribute(
                              'role'
                            ) === 'button' ||
                            control.hasAttribute(
                              'tabindex'
                            )
                          );
                      }

                      return {
                        index:
                          index,

                        tag:
                          control.tagName,

                        active:
                          active,

                        disabled:
                          disabled,

                        href_present:
                          Boolean(href),

                        valid_href:
                          validHref,

                        aria_disabled:
                          ariaDisabled,

                        pointer_events:
                          style.pointerEvents,

                        opacity:
                          style.opacity
                      };
                    }
                  );
                }
                """
            )

            counts_match = (
                len(blocks)
                == len(controls)
                and len(blocks) > 0
            )

            pairs = []

            if counts_match:
                for index in range(
                    len(blocks)
                ):
                    pairs.append(
                        {
                            "index": index,
                            "block": blocks[index],
                            "control": controls[index],
                        }
                    )

            result = {
                "terminated_blocks":
                    len(blocks),

                "test_controls":
                    len(controls),

                "counts_match":
                    counts_match,

                "pairs":
                    pairs,
            }

            print(
                "=== TERMINATE PROBE V3.5D ==="
            )

            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                )
            )

            print(
                "=== FINE TERMINATE PROBE ==="
            )

            core.best_effort_logout(
                page
            )

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()