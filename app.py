import json
import logging
import time
from curl_cffi import requests
from datetime import datetime, timedelta, date, timezone
from dateutil.relativedelta import relativedelta
from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any, List, Tuple, Dict

class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    BASE_URL: str = "https://my.e-consul.gov.ua"
    DRY_RUN: bool = False   # Use dummy data from example/ directory
    INSTITUTION_CODE: str = '1000000514'    # Генеральне консульство України в Нью-Йорку
    INTERVAL: int = 300 # check interval
    LOG_LEVEL: str = 'INFO'
    OPERATION_NAME: str ='Оформлення закордонного паспорта'
    TELEGRAM_BOT_TOKEN: str = ''
    TELEGRAM_CHAT_ID: str = ''
    TOKEN: str = ''
    USER_AGENT: str = ''
    #CONSUL_IPN_HASH='90ed02ab516eecbe60b758139ec26d32498df7f83e02d89be5a5ff69afa46e4c' #TODO: remove if not used

    @computed_field
    @property
    def headers(self) -> dict[str, str]:
        return {
            "token": self.TOKEN,
            "User-Agent": self.USER_AGENT,
            "Content-Type": "application/json"
        }

    def model_post_init(self, __context) -> None:
        if not self.TOKEN or not self.USER_AGENT:
            raise ValueError("Token or UserAgent are not set")


# Mapping Ukrainian day names to Python weekday integers (Monday is 0, Sunday is 6)
UA_DAYS_MAP = {
    "понеділок": 0,
    "вівторок": 1,
    "середа": 2,
    "четвер": 3,
    "пʼятниця": 4,
    "субота": 5,
    "неділя": 6
}


settings = Settings()
# Set up logging
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        # logging.FileHandler('monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class FreeSlotCalculator:
    def __init__(self, schedule: dict[str, Any], reserved: dict[str, Any], min_slot_minutes: int):
        self.min_slot_delta = timedelta(minutes=min_slot_minutes)
        self.schedule, self.non_working_times = self._parse_schedule(schedule)
        self.reserved_times = self._parse_reserved(reserved)

        # Merge all unavailable times to prevent overlapping logic bugs
        self.unavailable_blocks = self._merge_intervals(self.non_working_times + self.reserved_times)

    def _parse_schedule(self, schedule_raw: dict[str, Any]) -> Tuple[Dict[int, List[Tuple[int, int, int, int]]], List[Tuple[datetime, datetime]]]:
        """Reads schedule and returns working hours mapped by weekday, and specific non-working blocks."""
        # Map weekday -> List of (start_hour, start_min, end_hour, end_min)
        schedule = {}
        for block in schedule_raw.get("receptionCitizensTime", []):
            day_idx = UA_DAYS_MAP[block["workingDays"].lower()]
            start_h, start_m = map(int, block["workingHoursFrom"].split(":"))
            end_h, end_m = map(int, block["workingHoursTo"].split(":"))

            if day_idx not in schedule:
                schedule[day_idx] = []
            schedule[day_idx].append((start_h, start_m, end_h, end_m))

        non_working = []
        for nw in schedule_raw.get("nonWorkingTime", []):
            start = datetime.fromisoformat(nw["notWorkingDateAndHoursFrom"])
            end = datetime.fromisoformat(nw["notWorkingDateAndHoursTo"])
            non_working.append((start, end))

        return schedule, non_working

    def _parse_reserved(self, reserved_raw: dict[str, Any]) -> List[Tuple[datetime, datetime]]:
        """Reads already reserved slots into a list of datetime tuples."""
        reserved = []
        for slot in reserved_raw.get("data", {}).get("reservedSlots", []):
            start = datetime.fromisoformat(slot["receptionDateAndTimeFrom"])
            end = datetime.fromisoformat(slot["receptionDateAndTimeTo"])
            reserved.append((start, end))

        return reserved

    def _merge_intervals(self, intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
        """Sorts and merges overlapping datetime intervals."""
        if not intervals:
            return []

        intervals.sort(key=lambda x: x[0])
        merged = [intervals[0]]

        for current_start, current_end in intervals[1:]:
            last_start, last_end = merged[-1]
            if current_start <= last_end:
                merged[-1] = (last_start, max(last_end, current_end))
            else:
                merged.append((current_start, current_end))

        return merged

    def _is_excluded(self, current_date: date, exclusions: List[Tuple[date, date]]) -> bool:
        """Checks if a date falls within any of the excluded ranges."""
        return any(ex_start <= current_date <= ex_end for ex_start, ex_end in exclusions)

    def find_available_slots(self, start_date: date, end_date: date, exclusions: List[Tuple[date, date]], tz_offset_hours: int = -7) -> List[Tuple[datetime, datetime]]:
        """Main engine to search for free slots across the given horizon."""
        available_slots = []
        current_date = start_date
        tz_info = timezone(timedelta(hours=tz_offset_hours))

        while current_date <= end_date:
            if self._is_excluded(current_date, exclusions):
                current_date += timedelta(days=1)
                continue

            weekday = current_date.weekday()
            daily_working_blocks = self.schedule.get(weekday, [])

            for start_h, start_m, end_h, end_m in daily_working_blocks:
                # Construct timezone-aware bounds for this working shift
                work_start = datetime(current_date.year, current_date.month, current_date.day,
                                      start_h, start_m, tzinfo=tz_info)
                work_end = datetime(current_date.year, current_date.month, current_date.day,
                                    end_h, end_m, tzinfo=tz_info)

                # Slide a window to find valid slots
                current_time = work_start
                while current_time + self.min_slot_delta <= work_end:
                    slot_end = current_time + self.min_slot_delta
                    overlap = False

                    # Check if this potential slot hits any merged unavailable blocks
                    for u_start, u_end in self.unavailable_blocks:
                        if current_time < u_end and slot_end > u_start:
                            overlap = True
                            # Jump our cursor to the end of the unavailable block
                            current_time = u_end
                            break

                    if not overlap:
                        available_slots.append((current_time, slot_end))
                        current_time = slot_end  # Move to the next slot continuously

            current_date += timedelta(days=1)

        return available_slots

class UkrainianConsulateSlotMonitor:

    def __init__(self, **values: Any):
        pass

    def _get_schedule(self) -> dict:
        """Get the Consulate schedule"""
        if settings.DRY_RUN:
            with open('examples/schedule.json', 'r') as f:
                return json.load(f)
        url = f"{settings.BASE_URL}/external_reader"
        payload = {
            "service": "e-queue-register",
            "method": "public-calendar-get-actual-consuls-schedule",
            "filters": {
                "institutionCode": settings.INSTITUTION_CODE
            }
        }

        try:
            response = requests.post(
                url,
                headers=settings.headers,
                json=payload,
                impersonate='firefox',
            )
            response.raise_for_status()
            logger.debug('Schedule received')
            return response.json()
        except requests.RequestsError as e:
            logger.error(f"Error fetching schedule: {e}")
            raise

    def _normalize_schedule(self, schedule: dict[str, Any]) -> dict:
        """Simplify scheduler response and merge json blocks"""
        normalized = {
            'consulIpnHashes': [],
            'nonWorkingTime': [],
            'receptionCitizensTime': []
        }
        for block in [i['data'] for i in schedule['data']]:
            if [i for i in block['consularInstitutionService'] if i['name'] == settings.OPERATION_NAME]:
                normalized['consulIpnHashes'].append(block['consulIpnHash'])
                normalized['nonWorkingTime'].extend(block['nonWorkingTime'])
                normalized['receptionCitizensTime'].extend(block['receptionCitizensTime'])
        logger.debug(f"Schedule normalized. consulIpnHashes: {normalized['consulIpnHashes']}")
        return normalized

    def _get_reserved_slots(self, consul_hashes: list[str]):
        """Get the reserved slots list"""
        if settings.DRY_RUN:
            with open('examples/reserved_slots.json', 'r') as f:
                return json.load(f)
        url = f"{settings.BASE_URL}/external_reader"
        payload = {
            "service": "e-queue-register",
            "method": "public-get-consuls-reserved-slots",
            "filters": {
                "institutionCode": settings.INSTITUTION_CODE,
                "status": [1, 2, 4, 5], # TODO: check what will return if 3 is set
                "consulIpnHash": consul_hashes
            }
        }

        try:
            response = requests.post(
                url,
                headers=settings.headers,
                json=payload,
                impersonate='firefox',
            )
            response.raise_for_status()
            logger.debug('Reserved slots received')
            return response.json()
        except requests.RequestsError as e:
            logger.error(f"Error fetching reserved slots: {e}")
            raise

    def run(self):
        while True:
            try:
                # 1. Get schedule
                schedule_raw = self._get_schedule()
                schedule = self._normalize_schedule(schedule_raw)
                if not schedule['consulIpnHashes']:
                    logger.warning("No consuls found supporting the operation")
                    time.sleep(settings.INTERVAL)
                    continue

                # 2. Get already reserved slots
                reserved_slots = self._get_reserved_slots(schedule['consulIpnHashes'])

                # 3. Calculate available slots
                if reserved_slots['data']['status'] == 'found':
                    # 4. Define the time horizon: Today + 7 days up to Today + 6 months
                    today = date.today()
                    horizon_start = today + timedelta(days=7)
                    horizon_end = today + relativedelta(months=6)

                    # 5. Define Exclusions (DD.MM.YYYY)
                    exclusion_ranges = [
                        (date(2026, 8, 17), date(2026, 8, 23))
                    ]

                    # 6. Initialize engine (assuming 15 minutes as minimal slot length)
                    calculator = FreeSlotCalculator(schedule=schedule, reserved=reserved_slots, min_slot_minutes=10)

                    # 7. Generate valid slots
                    slots = calculator.find_available_slots(
                        start_date=horizon_start,
                        end_date=horizon_end,
                        exclusions=exclusion_ranges,
                        tz_offset_hours=-7 # Derived from your dataset's ISO strings
                    )

                    # 8. Output results
                    logger.info(f"Found {len(slots)} available slots.")
                    for start, end in slots[:10]: # Print first 10 for brevity
                        logger.info(f"Available: {start.isoformat()} to {end.isoformat()}")

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(settings.INTERVAL)


if __name__ == "__main__":
    try:
        monitor = UkrainianConsulateSlotMonitor()
        monitor.run()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")