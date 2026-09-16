from __future__ import annotations

import json

import monitor_runner_v3 as v3


def main() -> None:
    observations = v3.scrape_terminated_v35_once()

    if observations is None:
        result = {
            "snapshot_ok": False,
            "observations": 0,
        }
        print("=== TERMINATE PROBE V3.5 FINAL ===")
        print(json.dumps(result, indent=2))
        print("=== FINE TERMINATE PROBE ===")
        raise SystemExit(1)

    parsed = 0
    required = 0
    controls = 0
    identities = []

    active_controls = 0
    updating_rows = 0

    for observation in observations:
        lesson = observation.get("lesson")
        meta = observation.get("meta")
        control = observation.get("control")

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
                observation.get("contains_accedi_al_test"),
                observation.get("contains_test_label"),
                observation.get("contains_presence"),
                observation.get("contains_recording"),
            ]
        ):
            required += 1

        if isinstance(control, dict):
            controls += 1

            if control.get("active") is True:
                active_controls += 1

        if observation.get("contains_in_aggiornamento"):
            updating_rows += 1

    result = {
        "snapshot_ok": True,
        "observations": len(observations),
        "parsed_identities": parsed,
        "all_rows_parsed": (
            parsed == len(observations)
        ),
        "rows_with_required_signals": required,
        "all_required_signals_present": (
            required == len(observations)
        ),
        "controls_complete": (
            controls == len(observations)
        ),
        "unique_identity_schedules": len(
            set(identities)
        ),
        "all_identity_schedules_unique": (
            len(set(identities))
            == len(observations)
        ),
        "active_test_controls": active_controls,
        "updating_rows": updating_rows,
    }

    print("=== TERMINATE PROBE V3.5 FINAL ===")
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
            result["observations"] > 0,
            result["all_rows_parsed"],
            result["all_required_signals_present"],
            result["controls_complete"],
            result["all_identity_schedules_unique"],
        ]
    )

    if not green:
        raise SystemExit(1)


if __name__ == "__main__":
    main()