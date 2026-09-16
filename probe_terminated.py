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
        print("=== TERMINATE PROBE V3.5F ===")
        print(json.dumps(result, indent=2))
        print("=== FINE TERMINATE PROBE ===")
        raise SystemExit(1)

    identities = []
    required_signals_ok = True
    controls_ok = True
    parsed_ok = True

    active_controls = 0
    updating_rows = 0

    for observation in observations:
        lesson = observation.get("lesson")
        meta = observation.get("meta")
        control = observation.get("control")

        if not isinstance(lesson, v3.core.Lesson):
            parsed_ok = False
            continue

        if not isinstance(meta, dict):
            parsed_ok = False
            continue

        identity = v3.strict_identity_from_meta(
            lesson,
            meta,
        )

        if identity is None:
            parsed_ok = False
            continue

        identities.append(
            (
                lesson.date,
                lesson.start,
                lesson.end,
                identity[0],
                identity[1],
            )
        )

        if not (
            observation.get("contains_accedi_al_test")
            and observation.get("contains_test_label")
            and observation.get("contains_presence")
            and observation.get("contains_recording")
        ):
            required_signals_ok = False

        if not isinstance(control, dict):
            controls_ok = False
            continue

        if control.get("active") is True:
            active_controls += 1

        if observation.get("contains_in_aggiornamento"):
            updating_rows += 1

    unique_identity_schedules = len(set(identities))

    result = {
        "snapshot_ok": True,
        "observations": len(observations),
        "parsed_identities": len(identities),
        "all_rows_parsed": (
            parsed_ok
            and len(identities) == len(observations)
        ),
        "all_required_signals_present": required_signals_ok,
        "controls_complete": controls_ok,
        "unique_identity_schedules": unique_identity_schedules,
        "all_identity_schedules_unique": (
            unique_identity_schedules == len(observations)
        ),
        "active_test_controls": active_controls,
        "updating_rows": updating_rows,
    }

    print("=== TERMINATE PROBE V3.5F ===")
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
            result["snapshot_ok"],
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