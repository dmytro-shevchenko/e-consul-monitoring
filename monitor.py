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
from collections import Counter, defaultdict
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
load_dotenv(ENV_PATH)

API_URL = "https://my.e-consul.gov.ua/external_reader"
BOOKING_LINK = "https://e-consul.gov.ua/tasks/create/161374/161374001"

DEFAULT_INSTITUTION_CODE = "1000000514"
DEFAULT_CONSUL_IPN_HASH = "90ed02ab516eecbe60b758139ec26d32498df7f83e02d89be5a5ff69afa46e4c"

# Ukrainian weekday names → Python weekday number (Mon=0)
UA_WEEKDAY: dict[str, int] = {
    "понеділок": 0, "вівторок": 1, "середа": 2, "четвер": 3,
    "п'ятниця": 4, "пятниця": 4, "субота": 5, "неділя": 6,
}


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
    consul_ipn_hash: str
    interval: int

    # Wave-detection heuristic tuning (rarely need changing)
    slot_minutes: int = 10
    high_day_reservations: int = 20      # min reservations to treat a day as an "anchor"
    max_gap_zero_bridge_days: int = 15   # max calendar gap between two anchors to bridge
    min_zero_days_major_bridge: int = 7  # min zero days in that gap to qualify as a wave

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
            consul_ipn_hash=_s("CONSUL_IPN_HASH", DEFAULT_CONSUL_IPN_HASH),
            interval=interval,
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

    def fetch_schedule(self) -> dict | None:
        """Fetch this consul's weekly reception schedule."""
        data = self._post(
            "public-calendar-get-actual-consuls-schedule",
            {"institutionCode": self._cfg.institution_code},
        )
        if not data or "data" not in data:
            return None
        for item in data["data"]:
            inner = item.get("data", {})
            if inner.get("consulIpnHash") == self._cfg.consul_ipn_hash:
                return inner
        self.last_error = (
            f"public-calendar-get-actual-consuls-schedule: no consul matching "
            f"CONSUL_IPN_HASH={self._cfg.consul_ipn_hash[:12]}… "
            "(check INSTITUTION_CODE / hash in Settings)"
        )
        return None

    def fetch_reserved_slots(self) -> list[dict] | None:
        """Fetch all currently reserved slots for this consul."""
        data = self._post(
            "public-get-consuls-reserved-slots",
            {
                "institutionCode": self._cfg.institution_code,
                "status": [1, 2, 4, 5],
                "consulIpnHash": [self._cfg.consul_ipn_hash],
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


def per_day_counts(slots: list[dict], consul_hash: str) -> Counter[date]:
    """Count reserved slots per calendar day for the given consul hash."""
    counts: Counter[date] = Counter()
    for s in slots:
        if s.get("consulIpnHash") != consul_hash:
            continue
        raw = (s.get("receptionDateAndTimeFrom") or "")[:10]
        if len(raw) < 10:
            continue
        try:
            counts[date.fromisoformat(raw)] += 1
        except ValueError:
            pass
    return counts


def bookable_days(
    counts: Counter[date],
    high_threshold: int = 20,
    max_gap_days: int = 15,
    min_zero_span: int = 7,
) -> tuple[set[date], dict]:
    """
    Determine which calendar days the portal treats as part of the active booking wave.

    Algorithm:
      1. Anchors — days where reserved count >= high_threshold. These are days the
         portal opened a full slot grid; many slots are already taken.
      2. Bridges — zero-reservation days strictly between two consecutive anchors when:
           * the calendar gap is <= max_gap_days (same wave, not months apart), AND
           * the gap contains >= min_zero_span zero days (a 3-day weekend is excluded;
             a free working week of ~7 days qualifies).
      3. Wave cutoff — drop everything before the earliest qualifying bridge. Older
         anchor days belong to a past wave the portal no longer offers.

    Returns (bookable_dates, meta_dict).
    """
    anchors = sorted(d for d, c in counts.items() if c >= high_threshold)
    allowed: set[date] = set(anchors)
    first_bridge_start: date | None = None

    for a, b in zip(anchors, anchors[1:]):
        gap = (b - a).days
        if not (1 < gap <= max_gap_days):
            continue

        # All calendar days strictly between the two anchors that have 0 reservations
        zeros = [
            date.fromordinal(o)
            for o in range(a.toordinal() + 1, b.toordinal())
            if counts.get(date.fromordinal(o), 0) == 0
        ]
        if len(zeros) < min_zero_span:
            continue

        # Trim Mon–Fri zero days that are immediately adjacent to either anchor.
        # A working day right after anchor a (or right before anchor b) is likely
        # a "not yet opened" day from the old/new batch — not part of the free wave.
        # Weekend zeros (Sat/Sun) are safe to keep: they never generate slots anyway.
        if zeros[0].weekday() < 5:       # first zero is Mon–Fri → adjacent to a
            zeros = zeros[1:]
        if zeros and zeros[-1].weekday() < 5:  # last zero is Mon–Fri → adjacent to b
            zeros = zeros[:-1]
        if not zeros:
            continue

        allowed.update(zeros)
        if first_bridge_start is None or zeros[0] < first_bridge_start:
            first_bridge_start = zeros[0]

    # Drop days that precede the active wave
    if first_bridge_start is not None:
        allowed = {d for d in allowed if d >= first_bridge_start}

    return allowed, {
        "anchor_count": len(anchors),
        "wave_cutoff": first_bridge_start,
        "bookable_count": len(allowed),
    }


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
                nw_ranges.append((
                    datetime.fromisoformat(from_s),
                    datetime.fromisoformat(to_s),
                ))
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


def reserved_slot_keys(slots: list[dict]) -> set[str]:
    """
    Extract 'YYYY-MM-DDTHH:MM' keys from raw reserved-slot records.
    Uses datetime.fromisoformat to normalise whatever timestamp format the API returns
    (handles both 'T' and space separators, trailing seconds/microseconds, etc.).
    """
    keys: set[str] = set()
    for s in slots:
        raw = (s.get("receptionDateAndTimeFrom") or "").replace(" ", "T")
        if len(raw) < 16:
            continue
        try:
            keys.add(datetime.fromisoformat(raw[:19]).strftime("%Y-%m-%dT%H:%M"))
        except ValueError:
            keys.add(raw[:16])  # Best-effort fallback for unusual formats
    return keys


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
    Main entry point: fetch schedule + reserved slots, apply wave heuristic, return free slots.

    Returns (free_list, free_count, reserved_count, window_start, window_end, meta)
    or None on any API / parse error.
    """
    schedule = _client.fetch_schedule()
    if schedule is None:
        return None
    raw_slots = _client.fetch_reserved_slots()
    if raw_slots is None:
        return None

    counts = per_day_counts(raw_slots, _cfg.consul_ipn_hash)
    allowed, bk_meta = bookable_days(
        counts,
        high_threshold=_cfg.high_day_reservations,
        max_gap_days=_cfg.max_gap_zero_bridge_days,
        min_zero_span=_cfg.min_zero_days_major_bridge,
    )

    today = date.today()
    horizon = today + timedelta(days=365 * 3)
    allowed_in_window = {d for d in allowed if today <= d <= horizon}

    possible = generate_slots(schedule, allowed_in_window, _cfg.slot_minutes)
    reserved = reserved_slot_keys(raw_slots)
    free_sorted = sorted(possible - reserved)

    # reserved_count = slots that are possible but already taken (within window)
    reserved_count = len(possible) - len(free_sorted)

    anchors_in_window = sum(
        1 for d in allowed_in_window if counts.get(d, 0) >= _cfg.high_day_reservations
    )
    zeros_in_window = sum(1 for d in allowed_in_window if counts.get(d, 0) == 0)

    meta = {
        "high_anchor_days_total": bk_meta["anchor_count"],
        "high_anchor_days_in_window": anchors_in_window,
        "zero_days_bridged_in_window": zeros_in_window,
        "bookable_calendar_days_in_window": len(allowed_in_window),
        "high_day_reservations_threshold": _cfg.high_day_reservations,
        "max_gap_zero_bridge_days": _cfg.max_gap_zero_bridge_days,
        "min_zero_days_major_bridge": _cfg.min_zero_days_major_bridge,
        "portal_wave_cutoff_date": (
            bk_meta["wave_cutoff"].isoformat() if bk_meta["wave_cutoff"] else None
        ),
    }
    return free_sorted, len(free_sorted), reserved_count, today, horizon, meta


# ---------------------------------------------------------------------------
# Module singletons — reloaded after settings change
# ---------------------------------------------------------------------------

_cfg: Config = Config.from_env()
_client: ApiClient = ApiClient(_cfg)

# Kept as a module-level variable so `from monitor import INTERVAL` in app.py still works
INTERVAL: int = _cfg.interval


def reload_config() -> None:
    """Reload all settings from .env and refresh module singletons."""
    global _cfg, _client, INTERVAL
    _cfg = Config.from_env()
    _client = ApiClient(_cfg)
    INTERVAL = _cfg.interval


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
    consul = (consul_ipn_hash or "").strip()
    if not inst or not consul:
        return False, "INSTITUTION_CODE and CONSUL_IPN_HASH are required"
    if re.fullmatch(r"[0-9a-fA-F]{64}", consul) is None:
        return False, "CONSUL_IPN_HASH must be 64 hex characters"

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
