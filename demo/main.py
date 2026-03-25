import os
import re
import time
from curl_cffi import requests
import json
from datetime import datetime, timedelta, date
import logging
from itertools import chain
from typing import List, Dict, Any, Optional, Tuple, Set, Iterator

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('slot_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Weekday names in schedule API (Ukrainian); maps to datetime.weekday() (Mon=0).
_UK_WEEKDAY_INDEX = {
    "понеділок": 0,
    "вівторок": 1,
    "середа": 2,
    "четвер": 3,
    "п'ятниця": 4,
    "пʼятниця": 4,
    "субота": 5,
    "неділя": 6,
}


class UkrainianConsulateSlotMonitor:
    def __init__(self):
        self.base_url = "https://my.e-consul.gov.ua"
        self.institution_code = os.getenv("INSTITUTION_CODE", "1000000514")
        self.operation_name = os.getenv("OPERATION_NAME", "Оформлення закордонного паспорта")
        self.token = os.getenv("TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiI2OWJiNDkzNWM3Y2FmYjJhNzAwN2E2NDkiLCJhdXRoVG9rZW5zIjp7ImFjY2Vzc1Rva2VuIjoiZjk0ZDMxNjU5NGM1MzU0ODkzNmFkMjdmNTU2MTJkMjBiNWJiNTZlMDVjMDA4MTE5MWU3ZjcyYmIxODU4YjkxZiIsInJlZnJlc2hUb2tlbiI6IjRjYmUwZDFlMTg2ZTliNzg3ZTQzMDM1NGRiMDY0OTM5YjJkNDdhOGVmZGZmNGRiOWRjNjkwM2VhNmVlMDcxYjcifSwiaWF0IjoxNzc0MzM0MzU5LCJleHAiOjE3NzQ0MjA3NTl9.Ur3Yjbp5smFHFobwQJ71ZbPtmMFF6lmRtWpksXNbuS4")
        self.user_agent = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0")
        # TLS/JA3 fingerprint preset; align with the browser family used for USER_AGENT (see curl_cffi BrowserType).
        self.impersonate = os.getenv("IMPERSONATE", "firefox144")

        if not self.token or not self.user_agent:
            raise ValueError("TOKEN and USER_AGENT must be set in environment variables")

        self.schedule_horizon_days = int(os.getenv("SCHEDULE_HORIZON_DAYS", "90"))

    def _schedule_blocks(self, schedule_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """API rows wrap the consul record in ['data']; support that shape and a flat fallback."""
        blocks: List[Dict[str, Any]] = []
        for row in schedule_data.get("data", []):
            if not isinstance(row, dict):
                continue
            inner = row.get("data")
            if isinstance(inner, dict) and inner.get("consulIpnHash") is not None:
                blocks.append(inner)
            elif row.get("consulIpnHash") is not None:
                blocks.append(row)
        return blocks

    def _block_has_operation(self, block: Dict[str, Any]) -> bool:
        for svc in block.get("consularInstitutionService") or []:
            if isinstance(svc, dict) and svc.get("name") == self.operation_name:
                return True
        return False

    def _normalize_slot_time(self, t: str) -> str:
        parts = (t or "").strip().split(":")
        if len(parts) >= 2:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        return (t or "").strip()[:5]

    def _time_to_minutes(self, t: str) -> int:
        return sum(x * y for x, y in zip(map(int, self._normalize_slot_time(t).split(":")), (60, 1)))

    def _minutes_to_hhmm(self, m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    def _weekday_indices_from_working_days(self, s: str) -> Set[int]:
        out: Set[int] = set()
        for part in re.split(r"[,;]", s):
            key = part.strip().lower().replace("ʼ", "'")
            if key in _UK_WEEKDAY_INDEX:
                out.add(_UK_WEEKDAY_INDEX[key])
        return out

    def _reception_templates(
        self, reception_times: List[Dict[str, Any]]
    ) -> List[Tuple[Set[int], str, str]]:
        """Rows like workingDays + workingHoursFrom/To (weekly template)."""
        tpl: List[Tuple[Set[int], str, str]] = []
        for row in reception_times:
            wd = row.get("workingDays")
            wf = row.get("workingHoursFrom")
            wt = row.get("workingHoursTo")
            if not wd or not wf or not wt:
                continue
            days = self._weekday_indices_from_working_days(str(wd))
            if days:
                tpl.append((days, str(wf), str(wt)))
        return tpl

    def _iter_weekly_template_slots(
        self, reception_times: List[Dict[str, Any]], horizon_days: int
    ) -> Iterator[Tuple[str, str]]:
        templates = self._reception_templates(reception_times)
        if not templates:
            return
        today = date.today()
        for offset in range(1, horizon_days + 1):
            cur = today + timedelta(days=offset)
            ds = cur.strftime("%Y-%m-%d")
            wday = cur.weekday()
            for days_set, wf, wt in templates:
                if wday not in days_set:
                    continue
                start_m = self._time_to_minutes(wf)
                end_m = self._time_to_minutes(wt)
                for sm in range(start_m, end_m, 10):
                    if sm + 10 > end_m:
                        break
                    yield ds, self._minutes_to_hhmm(sm)

    def _iter_legacy_calendar_slots(
        self, reception_times: List[Dict[str, Any]]
    ) -> Iterator[Tuple[str, str]]:
        """Rows with explicit date + times[]."""
        for row in reception_times:
            if "date" not in row:
                continue
            ds = row["date"]
            if self.is_today(str(ds)):
                continue
            for t in row.get("times") or []:
                yield str(ds), self._normalize_slot_time(str(t))

    def _slot_overlaps_non_working(
        self, date_str: str, slot_time: str, non_working: List[Any]
    ) -> bool:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return True
        sm = self._time_to_minutes(slot_time)
        slot_end = sm + 10
        for nw in non_working:
            if not isinstance(nw, dict):
                continue
            a_raw, b_raw = nw.get("notWorkingDateAndHoursFrom"), nw.get("notWorkingDateAndHoursTo")
            if not a_raw or not b_raw:
                continue
            try:
                a = datetime.fromisoformat(str(a_raw))
                b = datetime.fromisoformat(str(b_raw))
            except ValueError:
                continue
            if a.date() != day:
                continue
            am = a.hour * 60 + a.minute
            bm = b.hour * 60 + b.minute
            if sm < bm and slot_end > am:
                return True
        return False

    def _reserved_start_date_time(self, slot: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        raw = slot.get("receptionDateAndTimeFrom")
        if isinstance(raw, str) and raw:
            try:
                dt = datetime.fromisoformat(raw)
                return (dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"))
            except ValueError:
                return None
        d, tm = slot.get("date"), slot.get("time")
        if d and tm:
            return (str(d), self._normalize_slot_time(str(tm)))
        return None

    def _reserved_starts_by_consul(self, reserved_slots_data: Dict[str, Any]) -> Dict[str, Set[Tuple[str, str]]]:
        """API shape: data: { status, reservedSlots: [...] }; legacy: data: [ {...}, ... ]."""
        by_hash: Dict[str, Set[Tuple[str, str]]] = {}
        data = reserved_slots_data.get("data")
        entries: List[Dict[str, Any]] = []
        if isinstance(data, list):
            entries = [x for x in data if isinstance(x, dict)]
        elif isinstance(data, dict):
            slots = data.get("reservedSlots")
            if isinstance(slots, list):
                entries = [x for x in slots if isinstance(x, dict)]
        for slot in entries:
            h = slot.get("consulIpnHash")
            if not h:
                continue
            start = self._reserved_start_date_time(slot)
            if not start:
                continue
            by_hash.setdefault(h, set()).add((start[0], self._normalize_slot_time(start[1])))
        return by_hash

    def get_schedule(self) -> Dict[str, Any]:
        """Get the schedule for the consulate"""
        url = f"{self.base_url}/external_reader"
        headers = {
            "token": self.token,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json"
        }

        payload = {
            "service": "e-queue-register",
            "method": "public-calendar-get-actual-consuls-schedule",
            "filters": {
                "institutionCode": self.institution_code
            }
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                impersonate=self.impersonate,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestsError as e:
            logger.error(f"Error fetching schedule: {e}")
            raise

    def get_reserved_slots(self, consul_hashes: List[str]) -> Dict[str, Any]:
        """Get reserved slots for the given consuls"""
        url = f"{self.base_url}/external_reader"
        headers = {
            "token": self.token,
            "User-Agent": self.user_agent,
            "Content-Type": "application/json"
        }

        payload = {
            "service": "e-queue-register",
            "method": "public-get-consuls-reserved-slots",
            "filters": {
                "institutionCode": self.institution_code,
                "status": [1, 2, 4, 5],
                "consulIpnHash": consul_hashes
            }
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                impersonate=self.impersonate,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestsError as e:
            logger.error(f"Error fetching reserved slots: {e}")
            raise

    def is_today(self, date_str: str) -> bool:
        """Check if the given date string represents today"""
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj.date() == datetime.now().date()
        except ValueError:
            return False

    def _week_range_label_from_monday(self, monday: date) -> str:
        sunday = monday + timedelta(days=6)
        return f"{monday.strftime('%d.%m.%Y')} — {sunday.strftime('%d.%m.%Y')}"

    def _sorted_week_range_labels(self, slots: List[Dict[str, Any]]) -> List[str]:
        """Calendar weeks (Mon–Sun) that contain at least one slot, sorted by week start."""
        mondays: Set[date] = set()
        for s in slots:
            raw = s.get("date")
            if not raw:
                continue
            try:
                d = datetime.strptime(str(raw), "%Y-%m-%d").date()
            except ValueError:
                continue
            mondays.add(d - timedelta(days=d.weekday()))
        return [self._week_range_label_from_monday(m) for m in sorted(mondays)]

    def find_free_slots(self, schedule_data: Dict[str, Any], reserved_slots_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Find free slots while ignoring today's slots"""
        # Process the schedule data to get consul blocks with operation
        consul_blocks = [
            block for block in self._schedule_blocks(schedule_data)
            if self._block_has_operation(block)
        ]

        reserved_starts = self._reserved_starts_by_consul(reserved_slots_data)
        horizon = self.schedule_horizon_days

        free_slots = []
        for block in consul_blocks:
            consul_hash = block["consulIpnHash"]
            reception_times = block.get("receptionCitizensTime") or []
            non_working_times = block.get("nonWorkingTime") or []

            seen: Set[Tuple[str, str]] = set()
            for date_str, slot_time in chain(
                self._iter_weekly_template_slots(reception_times, horizon),
                self._iter_legacy_calendar_slots(reception_times),
            ):
                nt = self._normalize_slot_time(slot_time)
                key = (date_str, nt)
                if key in seen:
                    continue
                seen.add(key)
                if self._slot_overlaps_non_working(date_str, nt, non_working_times):
                    continue
                if key in reserved_starts.get(consul_hash, set()):
                    continue
                free_slots.append({
                    "date": date_str,
                    "time": nt,
                    "datetime": f"{date_str} {nt}",
                    "consul_hash": consul_hash,
                })

        return free_slots

    def send_notification(self, slots: List[Dict[str, Any]]):
        """Send notification about new free slots"""
        if not slots:
            return

        message = f"🚨 Нові вільні терміни!\n"
        for slot in slots:
            message += f"- {slot['date']} о {slot['time']}\n"

        # Telegram notification (if configured)
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if telegram_token and telegram_chat_id:
            try:
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                payload = {
                    "chat_id": telegram_chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                }
                requests.post(url, json=payload, impersonate=self.impersonate)
                logger.info("Telegram notification sent")
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")

    def run(self):
        """Main monitoring loop"""
        last_slot_count = 0
        while True:
            try:
                # Get schedule
                schedule_data = self.get_schedule()

                # Get consul hashes that support the operation
                consul_hashes = [
                    block["consulIpnHash"]
                    for block in self._schedule_blocks(schedule_data)
                    if self._block_has_operation(block)
                ]

                # If no consuls found, wait and try again
                if not consul_hashes:
                    logger.warning("No consuls found supporting the operation")
                    time.sleep(300)
                    continue

                # Get reserved slots for all relevant consuls
                reserved_slots_data = self.get_reserved_slots(consul_hashes)

                # Find free slots (excluding today's dates)
                free_slots = self.find_free_slots(schedule_data, reserved_slots_data)

                current_slot_count = len(free_slots)
                week_labels = (
                    "; ".join(self._sorted_week_range_labels(free_slots))
                    if current_slot_count
                    else ""
                )

                if current_slot_count > last_slot_count:
                    logger.info(
                        "Found %s free slots (was %s). Weeks: %s",
                        current_slot_count,
                        last_slot_count,
                        week_labels,
                    )
                    self.send_notification(free_slots)
                    last_slot_count = current_slot_count
                elif current_slot_count == 0 and last_slot_count > 0:
                    logger.info("All previous free slots are now taken or invalid")
                    last_slot_count = 0
                else:
                    if current_slot_count:
                        logger.info(
                            "Current free slot count: %s. Weeks: %s",
                            current_slot_count,
                            week_labels,
                        )
                    else:
                        logger.info("Current free slot count: 0")

                # Wait for next check (5 minutes)
                time.sleep(300)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(60)  # Wait a minute before retrying

if __name__ == "__main__":
    try:
        monitor = UkrainianConsulateSlotMonitor()
        monitor.run()
    except ValueError as e:
        print(f"Configuration error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")