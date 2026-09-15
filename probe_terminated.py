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

            waited_for_signal = True

            try:
                page.wait_for_function(
                    r"""
                    () => {
                      const text =
                        (
                          document.body.innerText ||
                          ''
                        )
                        .replace(/\s+/g, ' ')
                        .trim();

                      return (
                        /accedi\s+al\s+test/i.test(text) ||
                        /in\s+aggiornamento/i.test(text) ||
                        /%\s*presenza/i.test(text)
                      );
                    }
                    """,
                    timeout=15_000,
                )

            except PlaywrightTimeoutError:
                waited_for_signal = False

            result = page.evaluate(
                r"""
                () => {
                  const norm = value =>
                    (value || '')
                    .replace(/\s+/g, ' ')
                    .trim();

                  const bodyText =
                    norm(
                      document.body.innerText || ''
                    );

                  const all =
                    Array.from(
                      document.querySelectorAll(
                        'body *'
                      )
                    );

                  const exact =
                    all.filter(el =>
                      /^accedi\s+al\s+test$/i.test(
                        norm(
                          el.innerText ||
                          el.textContent
                        )
                      )
                    );

                  const contains =
                    all.filter(el =>
                      /accedi\s+al\s+test/i.test(
                        norm(
                          el.innerText ||
                          el.textContent
                        )
                      )
                    );

                  const leaves =
                    contains.filter(el =>
                      !Array.from(
                        el.children
                      ).some(child =>
                        /accedi\s+al\s+test/i.test(
                          norm(
                            child.innerText ||
                            child.textContent
                          )
                        )
                      )
                    );

                  const source =
                    exact.length
                      ? exact
                      : leaves;

                  const inspect =
                    (el, index) => {

                      const style =
                        window.getComputedStyle(
                          el
                        );

                      const clickable =
                        el.closest(
                          [
                            'button',
                            'a',
                            '[role="button"]',
                            '[tabindex]'
                          ].join(',')
                        );

                      let clickableInfo = null;

                      if (clickable) {
                        const clickableStyle =
                          window.getComputedStyle(
                            clickable
                          );

                        const href =
                          clickable.getAttribute(
                            'href'
                          );

                        clickableInfo = {
                          same_element:
                            clickable === el,

                          tag:
                            clickable.tagName,

                          class_name:
                            String(
                              clickable.className ||
                              ''
                            ),

                          role:
                            clickable.getAttribute(
                              'role'
                            ),

                          tabindex:
                            clickable.getAttribute(
                              'tabindex'
                            ),

                          disabled_property:
                            clickable.disabled === true,

                          disabled_selector:
                            clickable.matches(
                              ':disabled'
                            ),

                          disabled_attribute:
                            clickable.hasAttribute(
                              'disabled'
                            ),

                          aria_disabled:
                            clickable.getAttribute(
                              'aria-disabled'
                            ),

                          href_present:
                            Boolean(href),

                          href_placeholder:
                            (
                              href === '#' ||
                              href === '' ||
                              (
                                typeof href ===
                                  'string' &&
                                href
                                .toLowerCase()
                                .startsWith(
                                  'javascript:'
                                )
                              )
                            ),

                          onclick_present:
                            clickable.hasAttribute(
                              'onclick'
                            ),

                          pointer_events:
                            clickableStyle
                            .pointerEvents,

                          cursor:
                            clickableStyle.cursor,

                          opacity:
                            clickableStyle.opacity
                        };
                      }

                      let ancestor =
                        el.parentElement;

                      let depth = 1;
                      let rowInfo = null;

                      while (
                        ancestor &&
                        depth <= 15
                      ) {
                        const text =
                          norm(
                            ancestor.innerText ||
                            ''
                          );

                        const useful =
                          (
                            /%\s*presenza/i
                            .test(text) ||
                            /\bTest\b/i
                            .test(text) ||
                            /\bInizio\b/i
                            .test(text) ||
                            /\bFine\b/i
                            .test(text)
                          );

                        if (useful) {
                          rowInfo = {
                            depth:
                              depth,

                            tag:
                              ancestor.tagName,

                            class_name:
                              String(
                                ancestor.className ||
                                ''
                              ),

                            contains_in_aggiornamento:
                              /in\s+aggiornamento/i
                              .test(text),

                            contains_presence:
                              /%\s*presenza/i
                              .test(text),

                            contains_test:
                              /\bTest\b/i
                              .test(text),

                            contains_start:
                              /\bInizio\b/i
                              .test(text),

                            contains_end:
                              /\bFine\b/i
                              .test(text),

                            contains_recording:
                              /registrazione/i
                              .test(text),

                            text_length:
                              text.length
                          };

                          break;
                        }

                        ancestor =
                          ancestor.parentElement;

                        depth++;
                      }

                      return {
                        index:
                          index,

                        element: {
                          tag:
                            el.tagName,

                          class_name:
                            String(
                              el.className ||
                              ''
                            ),

                          role:
                            el.getAttribute(
                              'role'
                            ),

                          tabindex:
                            el.getAttribute(
                              'tabindex'
                            ),

                          disabled_property:
                            el.disabled === true,

                          disabled_selector:
                            el.matches(
                              ':disabled'
                            ),

                          disabled_attribute:
                            el.hasAttribute(
                              'disabled'
                            ),

                          aria_disabled:
                            el.getAttribute(
                              'aria-disabled'
                            ),

                          pointer_events:
                            style.pointerEvents,

                          cursor:
                            style.cursor,

                          opacity:
                            style.opacity
                        },

                        clickable_ancestor:
                          clickableInfo,

                        row_ancestor:
                          rowInfo
                      };
                    };

                  return {
                    body_signals: {
                      contains_accedi_al_test:
                        /accedi\s+al\s+test/i
                        .test(bodyText),

                      contains_in_aggiornamento:
                        /in\s+aggiornamento/i
                        .test(bodyText),

                      contains_presence:
                        /%\s*presenza/i
                        .test(bodyText),

                      contains_test:
                        /\bTest\b/i
                        .test(bodyText),

                      body_text_length:
                        bodyText.length
                    },

                    exact_text_elements:
                      exact.length,

                    containing_elements:
                      contains.length,

                    leaf_elements:
                      leaves.length,

                    candidates:
                      source
                      .slice(0, 8)
                      .map(inspect)
                  };
                }
                """
            )

            result[
                "waited_for_rows_signal"
            ] = waited_for_signal

            print(
                "=== TERMINATE PROBE V3.5B ==="
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