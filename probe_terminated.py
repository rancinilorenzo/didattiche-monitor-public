from __future__ import annotations

import json

import monitor as core
from playwright.sync_api import sync_playwright


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
            core.settle_spa(page, 1800)

            result = page.evaluate(
                r"""
                () => {
                  const norm = value =>
                    (value || '')
                      .replace(/\s+/g, ' ')
                      .trim();

                  const buttons = Array.from(
                    document.querySelectorAll(
                      'button, a, [role="button"]'
                    )
                  ).filter(el =>
                    /accedi\s+al\s+test/i.test(
                      norm(
                        el.innerText ||
                        el.textContent
                      )
                    )
                  );

                  const rows = [];
                  const seen = new Set();

                  for (const button of buttons) {
                    let node = button;
                    let row = null;

                    for (
                      let depth = 0;
                      depth < 12 && node.parentElement;
                      depth++
                    ) {
                      node = node.parentElement;

                      const text = norm(
                        node.innerText || ''
                      );

                      if (
                        /\bInizio\b/i.test(text) &&
                        /\bFine\b/i.test(text) &&
                        /% presenza/i.test(text) &&
                        /\bTest\b/i.test(text)
                      ) {
                        row = node;
                        break;
                      }
                    }

                    if (!row) {
                      continue;
                    }

                    const rowText = norm(
                      row.innerText || ''
                    );

                    if (seen.has(rowText)) {
                      continue;
                    }

                    seen.add(rowText);

                    const style =
                      window.getComputedStyle(button);

                    const href =
                      button.getAttribute('href');

                    rows.push({
                      index: rows.length,

                      row_signals: {
                        contains_in_aggiornamento:
                          /in aggiornamento/i.test(
                            rowText
                          ),

                        contains_presence_label:
                          /% presenza/i.test(
                            rowText
                          ),

                        contains_test_label:
                          /\bTest\b/i.test(
                            rowText
                          ),

                        contains_recording:
                          /registrazione/i.test(
                            rowText
                          ),

                        row_text_length:
                          rowText.length
                      },

                      test_button: {
                        tag:
                          button.tagName,

                        disabled_property:
                          button.disabled === true,

                        disabled_matches_selector:
                          button.matches(':disabled'),

                        disabled_attribute:
                          button.hasAttribute(
                            'disabled'
                          ),

                        aria_disabled:
                          button.getAttribute(
                            'aria-disabled'
                          ),

                        class_name:
                          String(
                            button.className || ''
                          ),

                        role:
                          button.getAttribute('role'),

                        tabindex:
                          button.getAttribute(
                            'tabindex'
                          ),

                        href_present:
                          Boolean(href),

                        href_is_placeholder:
                          href === '#' ||
                          href === '' ||
                          (
                            typeof href === 'string' &&
                            href.toLowerCase().startsWith(
                              'javascript:'
                            )
                          ),

                        onclick_present:
                          button.hasAttribute(
                            'onclick'
                          ),

                        pointer_events:
                          style.pointerEvents,

                        cursor:
                          style.cursor,

                        opacity:
                          style.opacity
                      }
                    });

                    if (rows.length >= 8) {
                      break;
                    }
                  }

                  return {
                    number_of_test_buttons:
                      buttons.length,

                    sampled_rows:
                      rows
                  };
                }
                """
            )

            print("=== TERMINATE PROBE V3.5A ===")

            print(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                )
            )

            print("=== FINE TERMINATE PROBE ===")

            core.best_effort_logout(page)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()