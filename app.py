"""
CLI entry point: run the monitor loop in the foreground (no web UI).
For the web UI run: python web_app.py
"""
from monitor import (
    INTERVAL,
    get_free_slots,
    get_last_api_error,
    BOOKING_LINK,
)
from datetime import datetime
import time


def main() -> None:
    print(
        f"Monitor slots from today forward (every {INTERVAL}s). "
        "Two-request logic: schedule − reserved = free."
    )
    last_free_count: int | None = None

    while True:
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = get_free_slots()
            if result is None:
                detail = get_last_api_error()
                print(f"[Checked at {checked_at}] {detail or 'API or parse error (check 403/token, USER_AGENT in .env)'}")
            else:
                free_list, free_count, reserved_count, w_start, w_end, meta = result
                possible = free_count + reserved_count
                print(
                    f"[Checked at {checked_at}] Window {w_start} → {w_end}: "
                    f"{possible} possible, {reserved_count} reserved, {free_count} free "
                    f"(bookable days: {meta.get('bookable_calendar_days_in_window', '?')}, "
                    f"anchors: {meta.get('high_anchor_days_in_window', '?')})"
                )
                if free_count > 0:
                    if last_free_count is None or last_free_count == 0:
                        print(f"\n[ALERT] FREE SLOTS: {free_count} available. First: {free_list[0]} … {BOOKING_LINK}\n")
                    elif last_free_count is not None and free_count > last_free_count:
                        print(f"\n[ALERT] Free slots increased from {last_free_count} to {free_count}. {BOOKING_LINK}\n")
                last_free_count = free_count
        except Exception as e:
            print(f"[Checked at {checked_at}] Error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
