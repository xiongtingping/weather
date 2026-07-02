import json

import scan_weather_market as swm


def main() -> None:
    payload = {
        "requested_from": "manual_target_realization_probe",
        "include_event_keys": [
            {
                "slug": "highest-temperature-in-chengdu-on-june-18-2026",
                "event_date": "2026-06-18",
            }
        ],
    }
    result = swm.run_auto_trades_for_date("2026-06-17", job_payload=payload)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
