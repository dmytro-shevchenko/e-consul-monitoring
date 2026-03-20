# Ukrainian Consulate Slot Monitor

## 🎯 Purpose

Maintain a 24/7 monitoring script for passport renewal slots at the Consulate General of Ukraine in New York. 
The agent must detect cancellations or new openings and send immediate notifications.

## 🛠 Technical Context

The system uses the єКонсул (e-Consul) portal. Authentication is handled via ID.GOV.UA (Diia/BankID), which generates a short-lived session, 24 hours token.

## Endpoints & Parameters

- API URL: https://my.e-consul.gov.ua/external_reader
- Method: POST
- Institution Code (NYC): 1000000514
- Operation (example): `Оформлення закордонного паспорта` — must match `consularInstitutionService[].name` in the schedule response
- Optional `CONSUL_IPN_HASH`: limit to one consul (e.g. `90ed02ab…`); if unset, all consuls that list the operation are merged
- Service Type: e-queue-register

## 🔑 Required Credentials (User Provided)

Authentication uses the **token** request header (JWT), not cookies. 
The `examples/` HAR files show that successful requests to `external_reader` send 
a **token** header and **User-Agent**; no Cookie header is present.

1. **TOKEN**: The JWT sent in the `token` request header (session auth from Diia/ID.GOV.UA).
2. **USER_AGENT**: Must match the browser used to obtain the token (helps with Cloudflare).

The monitor loads **all** consul blocks from the institution schedule, keeps those whose `consularInstitutionService` includes **OPERATION_NAME** (exact string), merges `receptionCitizensTime` and `nonWorkingTime`, expands weekly hours into **10-minute** slots from **today** through a fixed horizon, subtracts reserved times for the involved consul(s), and treats the remainder as free. The live portal may still hide some dates in the UI.

You can set `TOKEN` and `USER_AGENT` as environment variables (e.g. via `.env`); see `.env.example`. 

**How to get TOKEN and USER_AGENT:** Log in to e-consul.gov.ua in the browser, open DevTools (F12) → **Network** → trigger 
a request to the slot/date page → click the request to `my.e-consul.gov.ua` (e.g. `external_reader`) → in **Request Headers** copy 
the value of the **token** header (long JWT string) and the **User-Agent** header.

## 🤖 Agent Logic & Workflow

1. Availability Check (schedule + reserved slots)

- Action: (1) `public-calendar-get-actual-consuls-schedule` — select blocks listing **OPERATION_NAME**, merge hours + non-working intervals; (2) `public-get-consuls-reserved-slots` with every **consulIpnHash** from those blocks (one request, multiple hashes).
- Logic: For each calendar day in the forward window whose weekday appears in the merged reception template, emit **10-minute** slots, drop overlaps with `nonWorkingTime`, subtract reserved slot starts → **free slots**.
- Alert Trigger: When free slots appear or the free count increases.

2. Session Maintenance

- Heartbeat: The script must run every 300 seconds (5 minutes).
- 403 Handling: If the API returns 403 Forbidden, the agent must notify the user that the token (JWT) has expired and request a new login / token update.
- IP Consistency: The script must run from the same IP address used to log in manually, or Cloudflare will reject the token.

3. Notification Payload

Optional **Telegram**: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (or Settings). The monitor POSTs to `api.telegram.org/bot…/sendMessage` with HTML formatting; leave either variable empty to disable.

When a slot is detected, the agent should trigger a notification (Telegram/Webhook) containing:

- Date/Time of slot.
- Current total count of reserved slots.
- The direct booking link: https://e-consul.gov.ua/tasks/create/161374/161374001

## Agent rules

- Keep code compact and easy to read
- Keep logic straightforward
- For more complex blocks add a comment with explanation

## 💻 Reference Implementation (Python)

```Python
# The agent should maintain a script with this structure:
payload = {
    "service": "e-queue-register",
    "method": "public-get-consuls-reserved-slots",
    "filters": {
        "institutionCode": "1000000514",
        "status": [1, 2, 4, 5],
        "consulIpnHash": ["…", "…"]  # all consul hashes that list OPERATION_NAME in the schedule
    }
}
```

## ⚠️ Known Constraints

- Cloudflare WAF: Do not decrease the polling interval below 3 minutes.
- Diia Sync: For male applicants 18–60, the portal checks the Reserve+ database. If the user's status is not updated, the API may return 0 slots or an error even with a valid token.
- Midnight Release: Monitor heavily between 17:00 and 18:00 EST (Midnight Kyiv time), as this is when new daily batches are released.
- User token lifetime is 24 hours.

## 🔄 Updates Required

The user must update this file (or env / app config) whenever:

- The token (JWT) has expired (new manual login and copy new token).
- The institutionCode changes (if checking a different consulate like DC or Chicago).
- The **OPERATION_NAME** string if monitoring a different service (must match the schedule API exactly).