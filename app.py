"""
CLI entry point: run the monitor loop in the foreground (no web UI).
For the web UI run: python web_app.py
"""
import time
from datetime import datetime

import monitor
from monitor import format_slot_display, get_free_slots, get_last_api_error, BOOKING_LINK


def main() -> None:
    print(
        f"Monitor slots from today forward (every {monitor.INTERVAL}s). "
        "Two-request logic: schedule − reserved = free."
    )
    last_free_count: int | None = None

    while True:
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = get_free_slots()
            if result is None:
                detail = get_last_api_error()
                print(f"[{checked_at}] {detail or 'API or parse error (check TOKEN, USER_AGENT in .env)'}")
            else:
                free_list, free_count, reserved_count, w_start, w_end, meta = result
                print(
                    f"[{checked_at}] Window {w_start} → {w_end}: "
                    f"{free_count + reserved_count} possible, {reserved_count} reserved, {free_count} free "
                    f"(bookable days: {meta.get('bookable_calendar_days_in_window', '?')}, "
                    f"anchors: {meta.get('high_anchor_days_in_window', '?')})"
                )
                if free_count > 0:
                    if last_free_count is None or last_free_count == 0:
                        print(
                            f"\n[ALERT] FREE SLOTS: {free_count} available. "
                            f"First: {format_slot_display(free_list[0])} … {BOOKING_LINK}\n"
                        )
                    elif last_free_count is not None and free_count > last_free_count:
                        print(f"\n[ALERT] Free slots increased from {last_free_count} to {free_count}. {BOOKING_LINK}\n")
                last_free_count = free_count
        except Exception as exc:
            print(f"[{checked_at}] Error: {exc}")

        # Read INTERVAL each iteration so it picks up any reload_config() changes
        time.sleep(monitor.INTERVAL)


if __name__ == "__main__":
    main()
