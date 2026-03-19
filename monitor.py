"""
Core monitoring logic for e-Consul slot checker.
Run loop in a thread with start/stop; expose current status for the web UI.
"""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime, date, time as dt_time, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv, dotenv_values, set_key
from curl_cffi import requests as curl_requests

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_BASE_DIR, ".env")
load_dotenv(ENV_PATH)

# Defaults (NYC passport queue; override via .env or web Settings)
DEFAULT_INSTITUTION_CODE = "1000000514"
DEFAULT_CONSUL_IPN_HASH = "90ed02ab516eecbe60b758139ec26d32498df7f83e02d89be5a5ff69afa46e4c"


def _read_interval() -> int:
    try:
        return max(60, min(86400, int(os.getenv("INTERVAL", "300"))))
    except ValueError:
        return 300


# Config (reloaded by reload_config() after .env changes)
TOKEN = (os.getenv("TOKEN", "PASTE_JWT_TOKEN_HERE") or "").strip().strip("'\"")
USER_AGENT = (os.getenv("USER_AGENT", "PASTE_USER_AGENT_HERE") or "").strip().strip("'\"")
COOKIES = os.getenv("COOKIES", "")
INSTITUTION_CODE = os.getenv("INSTITUTION_CODE", DEFAULT_INSTITUTION_CODE)
CONSUL_IPN_HASH = os.getenv("CONSUL_IPN_HASH", DEFAULT_CONSUL_IPN_HASH)
INTERVAL = _read_interval()
SLOT_MINUTES = 10
# How far ahead to expand weekly reception hours into concrete slot times (intersected with bookable days below)
SLOT_HORIZON_DAYS = 365 * 10
# Day is an "anchor" when the portal shows a full slot grid (enough reservations that day).
HIGH_DAY_RESERVATIONS = 20
# Fill zero-reservation days only between two consecutive anchors a<b if (b-a) is short (portal batch gap).
MAX_GAP_ZERO_BRIDGE_DAYS = 15
# Only treat a gap as a real “open wave” when enough zero days sit between busy anchors (weekends are ~3 zeros, span 5).
MIN_ZERO_DAYS_MAJOR_BRIDGE = 7
API_URL = "https://my.e-consul.gov.ua/external_reader"
BOOKING_LINK = "https://e-consul.gov.ua/tasks/create/161374/161374001"

# Last HTTP/connection error from external_reader (for UI / CLI diagnostics)
_last_api_error: str | None = None

UA_WEEKDAY = {
    "понеділок": 0, "вівторок": 1, "середа": 2, "четвер": 3,
    "п'ятниця": 4, "пятниця": 4, "субота": 5, "неділя": 6,
}


def get_booking_window() -> tuple[date, date]:
    """Inclusive date range from today through a long fixed horizon (slot expansion only; portal enforces real limits)."""
    start = date.today()
    end = start + timedelta(days=SLOT_HORIZON_DAYS)
    return start, end


def get_headers():
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": "https://e-consul.gov.ua/",
        "Content-Type": "application/json",
        "token": TOKEN,
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
    if COOKIES.strip():
        h["Cookie"] = COOKIES.strip()
    return h


def _api_post(method: str, filters: dict) -> dict | None:
    global _last_api_error
    _last_api_error = None
    try:
        r = curl_requests.post(
            API_URL, headers=get_headers(), json={
                "service": "e-queue-register",
                "method": method,
                "filters": filters,
            },
            timeout=15,
            impersonate="firefox",
        )
        if r.status_code != 200:
            body = r.text or ""
            if r.status_code in (401, 403):
                _last_api_error = _format_api_auth_error(method, r.status_code, body)
            else:
                snippet = body[:240].replace("\n", " ")
                _last_api_error = f"{method}: HTTP {r.status_code} {snippet}"
            return None
        try:
            return r.json()
        except ValueError as e:
            _last_api_error = f"{method}: invalid JSON ({e})"
            return None
    except curl_requests.RequestsError as e:
        _last_api_error = f"{method}: {e}"
        return None


def get_last_api_error() -> str | None:
    return _last_api_error


def decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload (no signature verification). Returns None if invalid."""
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
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_token_status() -> dict:
    """
    Expiry from JWT `exp` (Unix seconds, UTC).
    Keys: configured, expired, exp_unix, exp_utc_iso, exp_local_display, expires_in_human, issues, server_message_hint
    """
    out: dict = {
        "configured": False,
        "expired": False,
        "exp_unix": None,
        "exp_utc_iso": None,
        "exp_local_display": None,
        "expires_in_human": None,
        "issues": None,
    }
    tok = (TOKEN or "").strip().strip("'\"")
    if not tok or tok.startswith("PASTE_"):
        out["issues"] = "No TOKEN configured — add one in Settings."
        return out
    payload = decode_jwt_payload(tok)
    if payload is None:
        out["configured"] = True
        out["issues"] = "TOKEN is not a readable JWT (cannot show expiry)."
        return out
    out["configured"] = True
    exp = payload.get("exp")
    if exp is None:
        out["issues"] = "JWT has no exp claim."
        return out
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        out["issues"] = "Invalid exp in JWT."
        return out
    out["exp_unix"] = exp_i
    exp_dt = datetime.fromtimestamp(exp_i, tz=timezone.utc)
    out["exp_utc_iso"] = exp_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    local = exp_dt.astimezone()
    out["exp_local_display"] = local.strftime("%Y-%m-%d %H:%M:%S %Z")
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


def _format_api_auth_error(method: str, status_code: int, body_text: str) -> str:
    """Human-readable auth/API errors using response body + JWT expiry."""
    server_msg = ""
    try:
        j = json.loads(body_text)
        err = j.get("error") if isinstance(j.get("error"), dict) else None
        if err and err.get("message"):
            server_msg = str(err["message"]).strip()
        elif isinstance(j.get("message"), str):
            server_msg = j["message"].strip()
    except (json.JSONDecodeError, TypeError):
        pass

    ts = get_token_status()
    exp_line = ""
    if ts.get("exp_utc_iso"):
        if ts.get("expired"):
            exp_line = f" JWT expiry: {ts['exp_utc_iso']} (already passed)."
        else:
            exp_line = f" JWT expiry: {ts['exp_utc_iso']} (~{ts.get('expires_in_human', '?')} left)."
    elif ts.get("issues"):
        exp_line = f" ({ts['issues']})"

    if status_code == 401:
        core = (
            "Authentication failed (HTTP 401): the server rejected your TOKEN — usually expired or revoked."
            f"{exp_line}"
        )
        if server_msg:
            core += f' Portal message: "{server_msg}"'
        core += " → Open e-consul in the browser, log in, copy a fresh `token` header in Settings."
        return f"{method}: {core}"

    if status_code == 403:
        core = (
            f"Access denied (HTTP 403) — often Cloudflare or session mismatch.{exp_line}"
        )
        if server_msg:
            core += f' Server: "{server_msg}"'
        return f"{method}: {core}"

    snippet = (body_text or "")[:200].replace("\n", " ")
    return f"{method}: HTTP {status_code} {snippet}"


def fetch_schedule() -> dict | None:
    global _last_api_error
    data = _api_post("public-calendar-get-actual-consuls-schedule", {"institutionCode": INSTITUTION_CODE})
    if not data or "data" not in data:
        return None
    for item in data["data"]:
        inner = item.get("data", {})
        if inner.get("consulIpnHash") == CONSUL_IPN_HASH:
            return inner
    _last_api_error = (
        f"public-calendar-get-actual-consuls-schedule: no consul with CONSUL_IPN_HASH={CONSUL_IPN_HASH[:12]}… "
        f"(check INSTITUTION_CODE / hash in Settings or .env)"
    )
    return None


def fetch_reserved_slots() -> list[dict] | None:
    data = _api_post(
        "public-get-consuls-reserved-slots",
        {"institutionCode": INSTITUTION_CODE, "status": [1, 2, 4, 5], "consulIpnHash": [CONSUL_IPN_HASH]},
    )
    if not data or "data" not in data:
        return None
    return data["data"].get("reservedSlots", [])


def _per_day_reserved_counts(slots: list[dict], consul_hash: str) -> dict[date, int]:
    """Number of reserved slots per calendar day for this consul."""
    counts: dict[date, int] = {}
    for s in slots:
        if s.get("consulIpnHash") != consul_hash:
            continue
        fs = (s.get("receptionDateAndTimeFrom") or "")[:10]
        if len(fs) < 10:
            continue
        try:
            d = date.fromisoformat(fs)
        except ValueError:
            continue
        counts[d] = counts.get(d, 0) + 1
    return counts


def _allowed_booking_days_from_high_anchors(
    counts: dict[date, int],
    high_threshold: int,
    max_gap_calendar_days: int,
    min_zeros_major_bridge: int,
) -> tuple[set[date], int, int, date | None]:
    """
    Bookable days ≈ portal “waves”:
    - Anchor day: reserved count >= high_threshold (busy day ⇒ full grid was offered).
    - Bridge: calendar days with **zero** reservations strictly between two consecutive anchors
      a < b when (b - a).days <= max_gap_calendar_days **and** the gap contains at least
      ``min_zeros_major_bridge`` zero days. Small gaps (e.g. span 5 with ~3 weekend zeros)
      are **not** bridged — avoids phantom slots Mar–Jul while still opening Aug 12→24
      (10 zero days Aug 14–23 in sample data).

    Then **drop** every bookable day **before** the first calendar day that appears in any
    qualifying zero-bridge (portal only “shows” the current wave forward). If there is no
    major bridge yet, keep all anchors (no cutoff) so new consuls still work.

    Days with a few reservations but below threshold (e.g. 17) are excluded — no fake “free” slots.
    """
    high_days = sorted(d for d, c in counts.items() if c >= high_threshold)
    allowed: set[date] = set(high_days)
    zero_days_added = 0
    first_major_zero: date | None = None
    for i in range(len(high_days) - 1):
        a, b = high_days[i], high_days[i + 1]
        span = (b - a).days
        if span <= 1 or span > max_gap_calendar_days:
            continue
        gap_zeros: list[date] = []
        d = a + timedelta(days=1)
        while d < b:
            if counts.get(d, 0) == 0:
                gap_zeros.append(d)
            d += timedelta(days=1)
        if len(gap_zeros) < min_zeros_major_bridge:
            continue
        for z in gap_zeros:
            allowed.add(z)
        zero_days_added += len(gap_zeros)
        gz_min = min(gap_zeros)
        if first_major_zero is None or gz_min < first_major_zero:
            first_major_zero = gz_min
    cutoff = first_major_zero
    if cutoff is not None:
        allowed = {d for d in allowed if d >= cutoff}
    return allowed, len(high_days), zero_days_added, cutoff


def _parse_time(s: str) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _generate_possible_slots(
    schedule: dict,
    watch_start: date,
    watch_end: date,
    allowed_dates: set[date],
) -> set[str]:
    reception = schedule.get("receptionCitizensTime", [])
    non_working = schedule.get("nonWorkingTime", [])
    blocks: list[tuple[int, int, int]] = []
    for rec in reception:
        wd = UA_WEEKDAY.get((rec.get("workingDays") or "").strip())
        if wd is None:
            continue
        h1, m1 = _parse_time(rec.get("workingHoursFrom", "09:00"))
        h2, m2 = _parse_time(rec.get("workingHoursTo", "12:00"))
        start_m, end_m = h1 * 60 + m1, h2 * 60 + m2
        if start_m < end_m:
            blocks.append((wd, start_m, end_m))
    nw_ranges: list[tuple[date, dt_time, dt_time]] = []
    for nw in non_working:
        from_s = (nw.get("notWorkingDateAndHoursFrom") or "")[:19]
        to_s = (nw.get("notWorkingDateAndHoursTo") or "")[:19]
        if len(from_s) >= 19 and len(to_s) >= 19:
            try:
                dt_from = datetime.fromisoformat(from_s)
                dt_to = datetime.fromisoformat(to_s)
                nw_ranges.append((dt_from.date(), dt_from.time(), dt_to.time()))
            except (ValueError, TypeError):
                pass
    start_d, end_d = watch_start, watch_end
    possible: set[str] = set()
    delta = timedelta(minutes=SLOT_MINUTES)
    d = start_d
    while d <= end_d:
        if d not in allowed_dates:
            d += timedelta(days=1)
            continue
        wd = d.weekday()
        for (b_wd, start_m, end_m) in blocks:
            if wd != b_wd:
                continue
            start_t = dt_time(start_m // 60, start_m % 60)
            end_t = dt_time(end_m // 60, end_m % 60)
            slot_start = datetime.combine(d, start_t)
            end_dt = datetime.combine(d, end_t)
            while slot_start < end_dt:
                st = slot_start.time()
                skip = any(d == nw_d and nw_from <= st < nw_to for (nw_d, nw_from, nw_to) in nw_ranges)
                if not skip:
                    possible.add(slot_start.strftime("%Y-%m-%dT%H:%M"))
                slot_start += delta
        d += timedelta(days=1)
    return possible


def _reserved_slot_keys(slots: list[dict], watch_start: date, watch_end: date) -> set[str]:
    start_s = watch_start.isoformat()
    end_s = watch_end.isoformat()
    keys = set()
    for s in slots:
        from_s = (s.get("receptionDateAndTimeFrom") or "")
        if len(from_s) < 16:
            continue
        date_part = from_s[:10]
        if start_s <= date_part <= end_s:
            keys.add(from_s[:16].replace(".00", ""))
    return keys


def get_free_slots() -> tuple[list[str], int, int, date, date, dict] | None:
    """
    Returns (free_list, free_count, reserved_count, window_start, window_end, meta) or None on error.
    meta includes cluster/bookable-day stats for the UI.
    """
    watch_start, watch_end = get_booking_window()
    schedule = fetch_schedule()
    if not schedule:
        return None
    reserved_list = fetch_reserved_slots()
    if reserved_list is None:
        return None
    counts = _per_day_reserved_counts(reserved_list, CONSUL_IPN_HASH)
    bookable_all, anchor_count, _, wave_cutoff = _allowed_booking_days_from_high_anchors(
        counts,
        HIGH_DAY_RESERVATIONS,
        MAX_GAP_ZERO_BRIDGE_DAYS,
        MIN_ZERO_DAYS_MAJOR_BRIDGE,
    )
    allowed_dates = {d for d in bookable_all if watch_start <= d <= watch_end}
    possible = _generate_possible_slots(schedule, watch_start, watch_end, allowed_dates)
    reserved_keys = _reserved_slot_keys(reserved_list, watch_start, watch_end)
    free_sorted = sorted(possible - reserved_keys)
    anchors_in_window = sum(1 for d in allowed_dates if counts.get(d, 0) >= HIGH_DAY_RESERVATIONS)
    zero_bridged_in_window = sum(1 for d in allowed_dates if counts.get(d, 0) == 0)
    meta = {
        "high_anchor_days_total": anchor_count,
        "high_anchor_days_in_window": anchors_in_window,
        "zero_days_bridged_in_window": zero_bridged_in_window,
        "bookable_calendar_days_in_window": len(allowed_dates),
        "high_day_reservations_threshold": HIGH_DAY_RESERVATIONS,
        "max_gap_zero_bridge_days": MAX_GAP_ZERO_BRIDGE_DAYS,
        "min_zero_days_major_bridge": MIN_ZERO_DAYS_MAJOR_BRIDGE,
        "portal_wave_cutoff_date": wave_cutoff.isoformat() if wave_cutoff else None,
    }
    return free_sorted, len(free_sorted), len(reserved_keys), watch_start, watch_end, meta


def reload_config() -> None:
    """Reload all settings from .env (call after saving the file)."""
    global TOKEN, USER_AGENT, COOKIES, INSTITUTION_CODE, CONSUL_IPN_HASH, INTERVAL
    load_dotenv(ENV_PATH, override=True)
    TOKEN = (os.getenv("TOKEN", "PASTE_JWT_TOKEN_HERE") or "").strip().strip("'\"")
    USER_AGENT = (os.getenv("USER_AGENT", "PASTE_USER_AGENT_HERE") or "").strip().strip("'\"")
    COOKIES = os.getenv("COOKIES", "")
    INSTITUTION_CODE = os.getenv("INSTITUTION_CODE", DEFAULT_INSTITUTION_CODE)
    CONSUL_IPN_HASH = os.getenv("CONSUL_IPN_HASH", DEFAULT_CONSUL_IPN_HASH)
    INTERVAL = _read_interval()


def get_settings_for_form() -> dict:
    """Values for the web Settings form (syncs from .env first)."""
    reload_config()
    file_token = (dotenv_values(ENV_PATH).get("TOKEN") or "").strip()
    token_ok = bool(file_token) and not file_token.startswith("PASTE_")
    return {
        "user_agent": USER_AGENT,
        "cookies": COOKIES,
        "interval": INTERVAL,
        "institution_code": INSTITUTION_CODE,
        "consul_ipn_hash": CONSUL_IPN_HASH,
        "token_configured": token_ok,
        "env_path": ENV_PATH,
        "token_status": get_token_status(),
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
    """Persist settings to .env and reload in-process config. Empty token leaves existing TOKEN unchanged."""
    Path(ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not Path(ENV_PATH).is_file():
        Path(ENV_PATH).touch()

    try:
        iv = max(60, min(86400, int((interval or "300").strip() or "300")))
    except ValueError:
        return False, "INTERVAL must be a number (seconds, 60–86400)"

    ua = (user_agent or "").strip()
    if not ua:
        return False, "USER_AGENT is required"
    if len(ua) < 40:
        return False, "USER_AGENT looks incomplete — paste the full value from DevTools → Network (same request as TOKEN)"

    inst = (institution_code or "").strip()
    consul = (consul_ipn_hash or "").strip()
    if not inst or not consul:
        return False, "INSTITUTION_CODE and CONSUL_IPN_HASH are required"
    if re.fullmatch(r"[0-9a-fA-F]{64}", consul) is None:
        return False, "CONSUL_IPN_HASH must be 64 hex characters"

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


class Monitor:
    """Runs the check loop in a background thread; start/stop and status for the web UI."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_check_at: str | None = None
        self._last_result: dict | None = None  # possible, reserved, free_count, first_free
        self._last_error: str | None = None
        self._last_free_count: int | None = None

    def _run_loop(self) -> None:
        while not self._stop.wait(timeout=0):
            checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = get_free_slots()
                with self._lock:
                    self._last_check_at = checked_at
                    self._last_error = None
                    if result is None:
                        detail = get_last_api_error()
                        self._last_error = detail or "API or parse error (check TOKEN, USER_AGENT, network)."
                        self._last_result = None
                    else:
                        free_list, free_count, reserved_count, w_start, w_end, meta = result
                        possible = free_count + reserved_count
                        self._last_result = {
                            "possible": possible,
                            "reserved": reserved_count,
                            "free_count": free_count,
                            "first_free": free_list[0] if free_list else None,
                            "window_start": w_start.isoformat(),
                            "window_end": w_end.isoformat(),
                            **meta,
                        }
                        if free_count > 0:
                            if self._last_free_count is None or self._last_free_count == 0:
                                self._notify(f"FREE SLOTS: {free_count} available. First: {free_list[0]}")
                            elif self._last_free_count is not None and free_count > self._last_free_count:
                                self._notify(f"Free slots increased from {self._last_free_count} to {free_count}.")
                        self._last_free_count = free_count
            except Exception as e:
                with self._lock:
                    self._last_check_at = checked_at
                    self._last_error = str(e)
                    self._last_result = None
            self._stop.wait(timeout=INTERVAL)

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
            self._thread.join(timeout=INTERVAL + 5)
            self._thread = None
        return True

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "last_check_at": self._last_check_at,
                "last_result": self._last_result,
                "last_error": self._last_error,
                "token": get_token_status(),
            }


# Singleton for the web app
_monitor: Monitor | None = None


def get_monitor() -> Monitor:
    global _monitor
    if _monitor is None:
        _monitor = Monitor()
    return _monitor
