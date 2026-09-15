from __future__ import annotations

import json

import monitor as core
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


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
                [
                    "#password",
                    "input[type='password']",
                ],
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

            tab.click(timeout=10_000)
            core.settle_spa(page, 2000)

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

            result = page.evaluate(
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

                  const testLeaves =
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

                      return !Array.from(
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
                    });

                  const rows = [];
                  const rowSet = new Set();

                  for (const leaf of testLeaves) {
                    let node = leaf;
                    let row = null;
                    let depth = 0;

                    while (
                      node.parentElement &&
                      depth < 18
                    ) {
                      node =
                        node.parentElement;

                      depth++;

                      const text =
                        norm(
                          node.innerText ||
                          ''
                        );

                      const hasStart =
                        /\bInizio\b/i.test(
                          text
                        );

                      const hasEnd =
                        /\bFine\b/i.test(
                          text
                        );

                      const hasPresence =
                        /%\s*presenza/i.test(
                          text
                        );

                      const hasTest =
                        /\bTest\b/i.test(
                          text
                        );

                      if (
                        hasStart &&
                        hasEnd &&
                        hasPresence &&
                        hasTest &&
                        text.length <= 3000
                      ) {
                        row = node;
                        break;
                      }
                    }

                    if (!row) {
                      continue;
                    }

                    if (rowSet.has(row)) {
                      continue;
                    }

                    rowSet.add(row);

                    const text =
                      norm(
                        row.innerText ||
                        ''
                      );

                    const controls =
                      Array.from(
                        row.querySelectorAll(
                          'a, button, [role="button"]'
                        )
                      ).filter(el =>
                        /accedi\s+al\s+test/i
                        .test(
                          norm(
                            el.innerText ||
                            el.textContent
                          )
                        )
                      );

                    let activeLinks = 0;
                    let enabledButtons = 0;
                    let disabledButtons = 0;

                    for (
                      const control of controls
                    ) {
                      const label =
                        norm(
                          control.innerText ||
                          control.textContent
                        );

                      if (
                        !/accedi\s+al\s+test/i
                        .test(label)
                      ) {
                        continue;
                      }

                      if (
                        control.tagName === 'A'
                      ) {
                        const href =
                          control.getAttribute(
                            'href'
                          );

                        if (
                          href &&
                          href !== '#' &&
                          !href
                            .toLowerCase()
                            .startsWith(
                              'javascript:'
                            )
                        ) {
                          activeLinks++;
                        }
                      }

                      if (
                        control.tagName ===
                        'BUTTON'
                      ) {
                        const disabled =
                          control.disabled === true ||
                          control.matches(
                            ':disabled'
                          ) ||
                          control.hasAttribute(
                            'disabled'
                          ) ||
                          control.getAttribute(
                            'aria-disabled'
                          ) === 'true';

                        if (disabled) {
                          disabledButtons++;
                        } else {
                          enabledButtons++;
                        }
                      }
                    }

                    rows.push({
                      index:
                        rows.length,

                      ancestor_depth:
                        depth,

                      row_tag:
                        row.tagName,

                      row_class:
                        String(
                          row.className || ''
                        ),

                      contains_in_aggiornamento:
                        /in\s+aggiornamento/i
                        .test(text),

                      contains_start:
                        /\bInizio\b/i
                        .test(text),

                      contains_end:
                        /\bFine\b/i
                        .test(text),

                      contains_presence:
                        /%\s*presenza/i
                        .test(text),

                      contains_test:
                        /\bTest\b/i
                        .test(text),

                      contains_recording:
                        /registrazione/i
                        .test(text),

                      has_date_like_text:
                        /\b\d{1,2}\s+
                        (?:luned[iì]|marted[iì]|
                        mercoled[iì]|gioved[iì]|
                        venerd[iì]|sabato|domenica)
                        \s+[a-zàèéìòù]+\s+20\d{2}\b/ix
                        .test(text),

                      has_start_end_times:
                        /Inizio\s*\d{1,2}:\d{2}/i
                        .test(text) &&
                        /Fine\s*\d{1,2}:\d{2}/i
                        .test(text),

                      test_controls:
                        controls.length,

                      active_links:
                        activeLinks,

                      enabled_buttons:
                        enabledButtons,

                      disabled_buttons:
                        disabledButtons,

                      row_text_length:
                        text.length
                    });
                  }

                  return {
                    test_leaf_elements:
                      testLeaves.length,

                    unique_full_rows:
                      rows.length,

                    rows:
                      rows.slice(0, 12)
                  };
                }
                """
            )

            print(
                "=== TERMINATE PROBE V3.5C ==="
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

            core.best_effort_logout(page)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()