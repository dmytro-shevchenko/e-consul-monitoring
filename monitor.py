"""
Core monitoring logic for e-Consul slot checker.
Run loop in a thread with start/stop; expose current status for the web UI.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests as curl_requests
from dotenv import dotenv_values, load_dotenv, set_key

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).parent
ENV_PATH = str(_BASE_DIR / ".env")
EXAMPLES_DIR = _BASE_DIR / "examples"
# Fixed filenames for DRY_RUN (same shape as live API responses)
DRY_RUN_SCHEDULE_FILE = "response_2.json"
DRY_RUN_RESERVED_FILE = "response_1.json"
load_dotenv(ENV_PATH)

API_URL = "https://my.e-consul.gov.ua/external_reader"
BOOKING_LINK = "https://e-consul.gov.ua/tasks/create/161374/161374001"

DEFAULT_INSTITUTION_CODE = "1000000514"
DEFAULT_OPERATION_NAME = "Оформлення закордонного паспорта"

# Ukrainian weekday names → Python weekday number (Mon=0)
UA_WEEKDAY: dict[str, int] = {
    "понеділок": 0, "вівторок": 1, "середа": 2, "четвер": 3,
    "п'ятниця": 4, "пятниця": 4, "субота": 5, "неділя": 6,
}


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """All runtime settings in one place. Reload via Config.from_env()."""

    token: str
    user_agent: str
    cookies: str
    institution_code: str
    operation_name: str
    consul_ipn_hash: str  # optional filter: empty = all consuls that offer OPERATION_NAME
    interval: int
    dry_run: bool = False
    slot_minutes: int = 10  # API uses 10-minute slots

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(ENV_PATH, override=True)

        def _s(key: str, default: str = "") -> str:
            return (os.getenv(key, default) or "").strip().strip("'\"")

        try:
            interval = max(60, min(86400, int(os.getenv("INTERVAL", "300"))))
        except ValueError:
            interval = 300

        return cls(
            token=_s("TOKEN", "PASTE_JWT_TOKEN_HERE"),
            user_agent=_s("USER_AGENT", "PASTE_USER_AGENT_HERE"),
            cookies=_s("COOKIES"),
            institution_code=_s("INSTITUTION_CODE", DEFAULT_INSTITUTION_CODE),
            operation_name=_s("OPERATION_NAME", DEFAULT_OPERATION_NAME),
            consul_ipn_hash=_s("CONSUL_IPN_HASH"),
            interval=interval,
            dry_run=_env_truthy("DRY_RUN"),
        )

    @property
    def is_token_set(self) -> bool:
        return bool(self.token) and not self.token.startswith("PASTE_")


# ---------------------------------------------------------------------------
# JWT helpers (pure — no I/O, no global state)
# ---------------------------------------------------------------------------

def decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload without signature verification. Returns None if malformed."""
    t = (token or "").strip().strip("'\"")
    if not t or t.startswith("PASTE_"):
        return None
    parts = t.split(".")
    if len(parts) != 3:
        return None
    body = parts[1]
    pad = 4 - len(body) % 4
    if pad != 4:
        body += "=" * pad
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_token_status(token: str) -> dict:
    """
    Parse JWT `exp` claim and return a human-readable status dict.
    Keys: configured, expired, exp_unix, exp_utc_iso, exp_local_display,
          expires_in_human, issues.
    """
    out: dict = {
        "configured": False, "expired": False,
        "exp_unix": None, "exp_utc_iso": None,
        "exp_local_display": None, "expires_in_human": None, "issues": None,
    }
    tok = (token or "").strip().strip("'\"")
    if not tok or tok.startswith("PASTE_"):
        out["issues"] = "No TOKEN configured — add one in Settings."
        return out

    payload = decode_jwt_payload(tok)
    if payload is None:
        out["configured"] = True
        out["issues"] = "TOKEN is not a readable JWT (cannot show expiry)."
        return out

    out["configured"] = True
    try:
        exp_i = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        out["issues"] = "JWT has no valid exp claim."
        return out

    out["exp_unix"] = exp_i
    exp_dt = datetime.fromtimestamp(exp_i, tz=timezone.utc)
    out["exp_utc_iso"] = exp_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    out["exp_local_display"] = exp_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    now_ts = time.time()
    out["expired"] = exp_i < now_ts
    if out["expired"]:
        out["issues"] = f"JWT expired at {out['exp_utc_iso']} — get a new token from the browser."
    else:
        remaining = int(exp_i - now_ts)
        h, rem = divmod(remaining, 3600)
        m, s = divmod(rem, 60)
        out["expires_in_human"] = f"{h}h {m}m" if h else f"{m}m {s}s"
        out["issues"] = f"Valid; expires in ~{out['expires_in_human']} ({out['exp_utc_iso']})."

    return out


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ApiClient:
    """Thin wrapper around the e-Consul external_reader API endpoint."""

    def __init__(self, config: Config) -> None:
        self._cfg = config
        self.last_error: str | None = None

    def _load_example_json(self, filename: str) -> dict | None:
        path = EXAMPLES_DIR / filename
        if not path.is_file():
            self.last_error = f"DRY_RUN: missing {path}"
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"DRY_RUN: {path}: {exc}"
            return None

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "User-Agent": self._cfg.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": "https://e-consul.gov.ua/",
            "Content-Type": "application/json",
            "token": self._cfg.token,
            "Origin": "https://e-consul.gov.ua",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Priority": "u=4",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "TE": "trailers",
        }
        if self._cfg.cookies:
            h["Cookie"] = self._cfg.cookies
        return h

    def _post(self, method: str, filters: dict) -> dict | None:
        self.last_error = None
        try:
            r = curl_requests.post(
                API_URL,
                headers=self._headers(),
                json={"service": "e-queue-register", "method": method, "filters": filters},
                timeout=15,
                impersonate="firefox",
            )
        except curl_requests.RequestsError as exc:
            self.last_error = f"{method}: {exc}"
            return None

        if r.status_code != 200:
            body = r.text or ""
            if r.status_code in (401, 403):
                self.last_error = self._format_auth_error(method, r.status_code, body)
            else:
                self.last_error = f"{method}: HTTP {r.status_code} {body[:240].replace(chr(10), ' ')}"
            return None

        try:
            return r.json()
        except ValueError as exc:
            self.last_error = f"{method}: invalid JSON ({exc})"
            return None

    def _format_auth_error(self, method: str, status_code: int, body: str) -> str:
        """Build a human-readable auth error message from the response body + JWT status."""
        server_msg = ""
        try:
            j = json.loads(body)
            err = j.get("error") if isinstance(j.get("error"), dict) else None
            server_msg = (
                str(err["message"]).strip() if err and err.get("message")
                else str(j.get("message", "")).strip()
            )
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        ts = get_token_status(self._cfg.token)
        if ts.get("exp_utc_iso"):
            suffix = (
                f" JWT expiry: {ts['exp_utc_iso']} (already passed)." if ts["expired"]
                else f" JWT expiry: {ts['exp_utc_iso']} (~{ts.get('expires_in_human', '?')} left)."
            )
        else:
            suffix = f" ({ts.get('issues', '')})"

        if status_code == 401:
            core = (
                f"Authentication failed (HTTP 401): TOKEN rejected — usually expired or revoked.{suffix}"
                + (f' Portal message: "{server_msg}"' if server_msg else "")
                + " → Log in to e-consul in browser and copy a fresh token."
            )
        else:
            core = (
                f"Access denied (HTTP 403) — Cloudflare or session mismatch.{suffix}"
                + (f' Server: "{server_msg}"' if server_msg else "")
            )
        return f"{method}: {core}"

    def fetch_institution_schedule(self) -> dict | None:
        """Full institution payload: `data` is a list of consul schedule blocks."""
        if self._cfg.dry_run:
            data = self._load_example_json(DRY_RUN_SCHEDULE_FILE)
        else:
            data = self._post(
                "public-calendar-get-actual-consuls-schedule",
                {"institutionCode": self._cfg.institution_code},
            )
        if not data or "data" not in data:
            return None
        return data

    def fetch_reserved_slots(self, consul_hashes: list[str]) -> list[dict] | None:
        """Reserved slots for the given consul IPN hashes (single API call)."""
        if not consul_hashes:
            self.last_error = "public-get-consuls-reserved-slots: no consul hashes"
            return None
        if self._cfg.dry_run:
            data = self._load_example_json(DRY_RUN_RESERVED_FILE)
        else:
            data = self._post(
                "public-get-consuls-reserved-slots",
                {
                    "institutionCode": self._cfg.institution_code,
                    "status": [1, 2, 4, 5],
                    "consulIpnHash": consul_hashes,
                },
            )
        if not data or "data" not in data:
            return None
        return data["data"].get("reservedSlots", [])


# ---------------------------------------------------------------------------
# Scheduling logic — pure functions (no I/O, fully testable)
# ---------------------------------------------------------------------------

def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse 'HH:MM' or 'HH:MM:SS' string → (hours, minutes). Returns (0, 0) on failure."""
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _block_lists_operation(inner: dict, operation_name: str) -> bool:
    op = (operation_name or "").strip()
    if not op:
        return False
    for svc in inner.get("consularInstitutionService") or []:
        if (svc.get("name") or "").strip() == op:
            return True
    return False


def merge_schedule_for_operation(
    institution_rows: list,
    operation_name: str,
    consul_ipn_hash: str,
) -> tuple[dict | None, list[str], str | None]:
    """
    Collect every institution `data` block that lists this operation in consularInstitutionService,
    optionally narrowed to CONSUL_IPN_HASH. Merge receptionCitizensTime and nonWorkingTime.

    Returns (merged_schedule_dict, ordered_unique_consul_hashes, error_message).
    """
    want_hash = (consul_ipn_hash or "").strip()
    blocks: list[dict] = []
    for row in institution_rows:
        if not isinstance(row, dict):
            continue
        inner = row.get("data") or {}
        if not _block_lists_operation(inner, operation_name):
            continue
        if want_hash and inner.get("consulIpnHash") != want_hash:
            continue
        blocks.append(inner)

    if not blocks:
        hint = f" with CONSUL_IPN_HASH={want_hash[:12]}…" if want_hash else ""
        return None, [], f"No schedule lists operation {operation_name!r}{hint}"

    merged: dict = {"receptionCitizensTime": [], "nonWorkingTime": []}
    hashes: list[str] = []
    for inner in blocks:
        h = inner.get("consulIpnHash")
        if isinstance(h, str) and h and h not in hashes:
            hashes.append(h)
        merged["receptionCitizensTime"].extend(inner.get("receptionCitizensTime") or [])
        merged["nonWorkingTime"].extend(inner.get("nonWorkingTime") or [])

    if not hashes:
        return None, [], "Schedule blocks list the operation but consulIpnHash is missing"

    return merged, hashes, None


def reception_weekdays(schedule: dict) -> set[int]:
    """Weekday indices (Mon=0) that have at least one receptionCitizensTime row."""
    out: set[int] = set()
    for rec in schedule.get("receptionCitizensTime") or []:
        wd = UA_WEEKDAY.get((rec.get("workingDays") or "").strip())
        if wd is not None:
            out.add(wd)
    return out


def dates_in_window_for_weekdays(today: date, horizon: date, weekdays: set[int]) -> set[date]:
    """Every calendar day from today through horizon whose weekday is in weekdays."""
    out: set[date] = set()
    d = today
    one = timedelta(days=1)
    while d <= horizon:
        if d.weekday() in weekdays:
            out.add(d)
        d += one
    return out


def _naive_wall(dt: datetime) -> datetime:
    """Strip tz so slot intervals compare to API non-working windows as wall-clock."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def generate_slots(
    schedule: dict,
    allowed_dates: set[date],
    slot_minutes: int = 10,
) -> set[str]:
    """
    Expand the weekly reception schedule into concrete 'YYYY-MM-DDTHH:MM' timestamps
    for every day in allowed_dates.

    Key correctness fix: non-working periods (breaks, one-off holidays) are stored as
    full datetime pairs and blocked with a proper interval-overlap test —
        slot [slot_start, slot_end) is blocked when slot_start < nw_to AND slot_end > nw_from
    rather than the previous point-in-time check that missed slots overlapping a break boundary.

    Key performance fix: iterates only over sorted(allowed_dates) — O(N bookable days)
    instead of the previous O(HORIZON_DAYS) loop that walked the full 10-year range.
    """
    # Map weekday int → list of (start_minute, end_minute) blocks from the weekly schedule
    day_blocks: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for rec in schedule.get("receptionCitizensTime", []):
        wd = UA_WEEKDAY.get((rec.get("workingDays") or "").strip())
        if wd is None:
            continue
        h1, m1 = _parse_hhmm(rec.get("workingHoursFrom", ""))
        h2, m2 = _parse_hhmm(rec.get("workingHoursTo", ""))
        start_m, end_m = h1 * 60 + m1, h2 * 60 + m2
        if start_m < end_m:
            day_blocks[wd].append((start_m, end_m))

    # Non-working periods as full datetime pairs for correct overlap arithmetic
    nw_ranges: list[tuple[datetime, datetime]] = []
    for nw in schedule.get("nonWorkingTime", []):
        from_s = (nw.get("notWorkingDateAndHoursFrom") or "")[:19]
        to_s = (nw.get("notWorkingDateAndHoursTo") or "")[:19]
        if len(from_s) >= 19 and len(to_s) >= 19:
            try:
                nw_ranges.append(
                    (_naive_wall(datetime.fromisoformat(from_s)), _naive_wall(datetime.fromisoformat(to_s)))
                )
            except (ValueError, TypeError):
                pass

    slot_delta = timedelta(minutes=slot_minutes)
    possible: set[str] = set()

    for d in sorted(allowed_dates):  # O(N) — iterate bookable days only
        for start_m, end_m in day_blocks.get(d.weekday(), []):
            slot_dt = datetime(d.year, d.month, d.day, start_m // 60, start_m % 60)
            end_dt = datetime(d.year, d.month, d.day, end_m // 60, end_m % 60)
            while slot_dt < end_dt:
                slot_end = slot_dt + slot_delta
                # Full interval overlap: blocked when [slot_dt, slot_end) ∩ [nw_from, nw_to) ≠ ∅
                blocked = any(slot_dt < nw_to and slot_end > nw_from for nw_from, nw_to in nw_ranges)
                if not blocked:
                    possible.add(slot_dt.strftime("%Y-%m-%dT%H:%M"))
                slot_dt = slot_end

    return possible


def _parse_api_datetime(raw: str) -> datetime | None:
    """Parse reservation / API timestamp to naive wall-clock datetime for interval tests."""
    s = (raw or "").strip().replace(" ", "T")
    if not s:
        return None
    for candidate in (s, s[:26], s[:19]):
        if len(candidate) < 19:
            continue
        try:
            return _naive_wall(datetime.fromisoformat(candidate))
        except ValueError:
            continue
    return None


def reserved_busy_intervals(slots: list[dict], consul_hashes: set[str] | None) -> list[tuple[datetime, datetime]]:
    """Busy [from, to) intervals from reserved slots (any duration)."""
    out: list[tuple[datetime, datetime]] = []
    for s in slots:
        h = s.get("consulIpnHash")
        if consul_hashes is not None and h not in consul_hashes:
            continue
        fr = _parse_api_datetime(s.get("receptionDateAndTimeFrom") or "")
        to = _parse_api_datetime(s.get("receptionDateAndTimeTo") or "")
        if fr is None or to is None or fr >= to:
            continue
        out.append((fr, to))
    return out


def _intervals_overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    """[a0,a1) overlaps [b0,b1) (half-open)."""
    return a0 < b1 and a1 > b0


def _reserved_intervals_by_calendar_day(
    reserved_intervals: list[tuple[datetime, datetime]],
) -> dict[date, list[tuple[datetime, datetime]]]:
    """Map each calendar day to reservations that intersect that day (for fast slot lookup)."""
    by_day: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
    one = timedelta(days=1)
    for fr, to in reserved_intervals:
        d = fr.date()
        end_d = to.date()
        while d <= end_d:
            by_day[d].append((fr, to))
            d += one
    return by_day


def possible_slots_not_overlapping_reservations(
    possible: set[str],
    reserved_intervals: list[tuple[datetime, datetime]],
    slot_minutes: int,
) -> set[str]:
    """
    Keep each slot_minutes grid slot [t, t+slot) only if it does not overlap any busy reservation.
    Reservations may be longer or shorter than slot_minutes (e.g. 15 min booking blocks 09:40).
    """
    delta = timedelta(minutes=slot_minutes)
    by_day = _reserved_intervals_by_calendar_day(reserved_intervals)
    free: set[str] = set()
    for key in possible:
        ss = _slot_key_datetime(key)
        if ss is None:
            continue
        se = ss + delta
        candidates = by_day.get(ss.date(), [])
        if any(_intervals_overlap(ss, se, rf, rt) for rf, rt in candidates):
            continue
        free.add(key)
    return free


def _slot_key_datetime(slot_key: str) -> datetime | None:
    try:
        return datetime.fromisoformat(slot_key.strip().replace(" ", "T")[:19])
    except ValueError:
        return None


def _slot_starts_at_or_after_now(slot_key: str, *, now: datetime | None = None) -> bool:
    """True if slot start is >= current local time rounded down to the minute (bookable / not in the past)."""
    dt = _slot_key_datetime(slot_key)
    if dt is None:
        return True
    t = now if now is not None else datetime.now()
    t = t.replace(second=0, microsecond=0)
    return dt >= t


def format_slot_display(slot: str) -> str:
    """
    Format an internal slot key for human-readable display.

    Internal keys use ``YYYY-MM-DDTHH:MM``; UI shows ``YYYY-MM-DD HH:MM`` (24-hour).
    """
    if not slot:
        return slot
    normalized = slot.strip().replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(normalized[:19]).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return slot.replace("T", " ", 1)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def get_free_slots() -> tuple[list[str], int, int, date, date, dict] | None:
    """
    Schedule for OPERATION_NAME (merged across matching consul blocks) minus reserved = free.

    A 10-minute candidate is free only if [start, start+10m) does not overlap any reservation
    [from, to) (bookings may be any length).

    Returned free slots exclude intervals whose start is before the current local minute
    (so the first slot is never a past time on the same day).

    Returns (free_list, free_count, reserved_count, window_start, window_end, meta)
    or None on any API / parse error.
    """
    payload = _client.fetch_institution_schedule()
    if payload is None:
        return None

    schedule, consul_hashes, err = merge_schedule_for_operation(
        payload["data"],
        _cfg.operation_name,
        _cfg.consul_ipn_hash,
    )
    if err or schedule is None:
        _client.last_error = err or "merge_schedule_for_operation failed"
        return None

    raw_slots = _client.fetch_reserved_slots(consul_hashes)
    if raw_slots is None:
        return None

    today = date.today()
    horizon = today + timedelta(days=365 * 3)
    wdays = reception_weekdays(schedule)
    allowed_in_window = dates_in_window_for_weekdays(today, horizon, wdays)

    possible = generate_slots(schedule, allowed_in_window, _cfg.slot_minutes)
    hash_set = set(consul_hashes)
    busy = reserved_busy_intervals(raw_slots, hash_set)
    free_in_grid = possible_slots_not_overlapping_reservations(possible, busy, _cfg.slot_minutes)
    reserved_count = len(possible) - len(free_in_grid)
    # Drop same-day (or earlier) slots already in the past; alerts only care about bookable times.
    now = datetime.now()
    free_sorted = sorted(s for s in free_in_grid if _slot_starts_at_or_after_now(s, now=now))

    meta = {
        "operation_name": _cfg.operation_name,
        "consul_ipn_hashes": consul_hashes,
        "calendar_days_in_window": len(allowed_in_window),
        "reception_weekdays": sorted(wdays),
    }
    return free_sorted, len(free_sorted), reserved_count, today, horizon, meta


# ---------------------------------------------------------------------------
# Module singletons — reloaded after settings change
# ---------------------------------------------------------------------------

_cfg: Config = Config.from_env()
_client: ApiClient = ApiClient(_cfg)

# Kept as module-level variables so `from monitor import INTERVAL` in app.py still works
INTERVAL: int = _cfg.interval
DRY_RUN: bool = _cfg.dry_run
OPERATION_NAME: str = _cfg.operation_name


def reload_config() -> None:
    """Reload all settings from .env and refresh module singletons."""
    global _cfg, _client, INTERVAL, DRY_RUN, OPERATION_NAME
    _cfg = Config.from_env()
    _client = ApiClient(_cfg)
    INTERVAL = _cfg.interval
    DRY_RUN = _cfg.dry_run
    OPERATION_NAME = _cfg.operation_name


def get_last_api_error() -> str | None:
    return _client.last_error


# ---------------------------------------------------------------------------
# Settings persistence (used by web UI)
# ---------------------------------------------------------------------------

def get_settings_for_form() -> dict:
    """Return current settings dict for the web Settings form."""
    reload_config()
    file_token = (dotenv_values(ENV_PATH).get("TOKEN") or "").strip()
    return {
        "user_agent": _cfg.user_agent,
        "cookies": _cfg.cookies,
        "interval": _cfg.interval,
        "institution_code": _cfg.institution_code,
        "operation_name": _cfg.operation_name,
        "consul_ipn_hash": _cfg.consul_ipn_hash,
        "token_configured": bool(file_token) and not file_token.startswith("PASTE_"),
        "env_path": ENV_PATH,
        "token_status": get_token_status(_cfg.token),
    }


def save_settings_from_form(
    *,
    token: str | None,
    user_agent: str,
    cookies: str,
    interval: str,
    institution_code: str,
    operation_name: str,
    consul_ipn_hash: str,
) -> tuple[bool, str]:
    """Validate, persist to .env, and hot-reload config. Empty token leaves TOKEN unchanged."""
    try:
        iv = max(60, min(86400, int((interval or "300").strip() or "300")))
    except ValueError:
        return False, "INTERVAL must be a number (seconds, 60–86400)"

    ua = (user_agent or "").strip()
    if not ua:
        return False, "USER_AGENT is required"
    if len(ua) < 40:
        return False, "USER_AGENT looks incomplete — paste the full value from DevTools → Network"

    inst = (institution_code or "").strip()
    op = (operation_name or "").strip()
    consul = (consul_ipn_hash or "").strip()
    if not inst:
        return False, "INSTITUTION_CODE is required"
    if not op:
        return False, "OPERATION_NAME is required"
    if consul and re.fullmatch(r"[0-9a-fA-F]{64}", consul) is None:
        return False, "CONSUL_IPN_HASH must be empty or 64 hex characters"

    Path(ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not Path(ENV_PATH).is_file():
        Path(ENV_PATH).touch()

    token_stripped = (token or "").strip()
    if token_stripped:
        set_key(ENV_PATH, "TOKEN", token_stripped, quote_mode="always")

    set_key(ENV_PATH, "USER_AGENT", ua, quote_mode="always")
    set_key(ENV_PATH, "COOKIES", cookies.strip() if cookies else "", quote_mode="always")
    set_key(ENV_PATH, "INTERVAL", str(iv), quote_mode="always")
    set_key(ENV_PATH, "INSTITUTION_CODE", inst, quote_mode="always")
    set_key(ENV_PATH, "OPERATION_NAME", op, quote_mode="always")
    set_key(ENV_PATH, "CONSUL_IPN_HASH", consul, quote_mode="always")

    reload_config()
    return True, f"Saved to {ENV_PATH}"


# ---------------------------------------------------------------------------
# Monitor — background loop
# ---------------------------------------------------------------------------

class Monitor:
    """Runs the slot-check loop in a daemon thread; exposes status for the web UI."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_check_at: str | None = None
        self._last_result: dict | None = None
        self._last_error: str | None = None
        self._last_free_count: int | None = None

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = get_free_slots()
                with self._lock:
                    self._last_check_at = checked_at
                    self._last_error = None
                    if result is None:
                        self._last_error = get_last_api_error() or "API or parse error."
                        self._last_result = None
                    else:
                        free_list, free_count, reserved_count, w_start, w_end, meta = result
                        self._last_result = {
                            "possible": free_count + reserved_count,
                            "reserved": reserved_count,
                            "free_count": free_count,
                            "first_free": (
                                format_slot_display(free_list[0]) if free_list else None
                            ),
                            "window_start": w_start.isoformat(),
                            "window_end": w_end.isoformat(),
                            **meta,
                        }
                        prev = self._last_free_count
                        if free_count > 0:
                            if prev is None or prev == 0:
                                self._notify(
                                    f"FREE SLOTS: {free_count} available. "
                                    f"First: {format_slot_display(free_list[0])}"
                                )
                            elif free_count > prev:
                                self._notify(f"Free slots increased from {prev} to {free_count}.")
                        self._last_free_count = free_count
            except Exception as exc:
                with self._lock:
                    self._last_check_at = checked_at
                    self._last_error = str(exc)
                    self._last_result = None

            # Wait for the configured interval or until stop is signalled
            self._stop.wait(timeout=_cfg.interval)

    def _notify(self, message: str) -> None:
        print(f"\n[ALERT] {message}\n")

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._running = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=_cfg.interval + 5)
            self._thread = None
        return True

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "last_check_at": self._last_check_at,
                "last_result": dict(self._last_result) if self._last_result else None,
                "last_error": self._last_error,
                "token": get_token_status(_cfg.token),
            }


# Singleton used by the web app
_monitor_singleton: Monitor | None = None


def get_monitor() -> Monitor:
    global _monitor_singleton
    if _monitor_singleton is None:
        _monitor_singleton = Monitor()
    return _monitor_singleton
