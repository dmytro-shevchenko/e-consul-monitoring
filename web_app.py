"""
Simple web UI: start/stop monitoring, status, and Settings (saved to .env).
Run: python web_app.py  then open http://127.0.0.1:8080
"""
import os
import secrets

from flask import (
    Flask,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from monitor import (
    BOOKING_LINK,
    get_monitor,
    get_settings_for_form,
    save_settings_from_form,
)

# Loads .env (via monitor import); WEB_USERNAME + WEB_PASSWORD optional single-user gate
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-e-consul-monitor-change-me")


def _web_auth_configured() -> bool:
    return bool((os.getenv("WEB_USERNAME") or "").strip() and (os.getenv("WEB_PASSWORD") or "").strip())


def _web_session_ok() -> bool:
    return session.get("web_auth") is True


def _web_check_credentials(username: str, password: str) -> bool:
    u_exp = (os.getenv("WEB_USERNAME") or "").strip()
    p_exp = (os.getenv("WEB_PASSWORD") or "").strip()
    u_in, p_in = username.encode("utf-8"), password.encode("utf-8")
    u_ok, p_ok = u_exp.encode("utf-8"), p_exp.encode("utf-8")
    if len(u_in) != len(u_ok) or len(p_in) != len(p_ok):
        return False
    return secrets.compare_digest(u_in, u_ok) and secrets.compare_digest(p_in, p_ok)


def nav_html() -> str:
    s = '<p style="margin-top:1rem;"><a href="/">Dashboard</a> · <a href="/settings">Settings</a>'
    if _web_auth_configured():
        s += ' · <a href="/logout">Log out</a>'
    return s + "</p>"

INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>e-Consul Slot Monitor</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; margin-bottom: 1rem; }
    .row { display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; }
    button { padding: 0.5rem 1rem; cursor: pointer; font-size: 1rem; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    #status { padding: 1rem; background: #f5f5f5; border-radius: 6px; font-size: 0.9rem; line-height: 1.5; }
    #status .sep { margin: 0.75rem 0; border: none; border-top: 1px solid #ccc; }
    .error { color: #c00; }
    .flash { padding: 0.75rem; background: #e8f5e9; border-radius: 6px; margin-bottom: 1rem; }
    .flash.err { background: #ffebee; }
    a { color: #06c; }
  </style>
</head>
<body>
  <h1>e-Consul Slot Monitor</h1>
  {% for c, m in get_flashed_messages(with_categories=true) %}
  <div class="flash {{ 'err' if c == 'error' else '' }}">{{ m }}</div>
  {% endfor %}
  <p>Monitoring from <strong>today</strong> across a long forward horizon (weekly schedule expanded into slots; the portal decides which dates are actually bookable).</p>
  <div class="row">
    <button id="btnStart" onclick="start()">Start</button>
    <button id="btnStop" onclick="stop()">Stop</button>
  </div>
  <div id="status">Loading status…</div>
  <p style="margin-top: 1rem;"><a href="{{ booking_link }}" target="_blank">Book on e-consul.gov.ua</a></p>
  {{ nav_html|safe }}
  <script>
    function escapeHtml(s) {
      if (!s) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function refresh() {
      fetch('/status', { credentials: 'same-origin' }).then(r => r.json()).then(d => {
        const s = document.getElementById('status');
        const run = d.running;
        document.getElementById('btnStart').disabled = run;
        document.getElementById('btnStop').disabled = !run;
        let html = '';
        html += '<div>Last check: ' + escapeHtml(d.last_check_at || '—') + '</div>';
        let validLine = '—';
        if (d.token) {
          const t = d.token;
          if (t.expired) validLine = '<span class="error">expired — update TOKEN in Settings</span>';
          else if (t.expires_in_human) validLine = '~' + escapeHtml(t.expires_in_human) + ' more';
          else if (t.issues) validLine = escapeHtml(t.issues);
        }
        html += '<div>Valid for: ' + validLine + '</div>';
        if (!run) {
          html += '<div class="error" style="margin-top:0.5rem;">Monitor is stopped — click <strong>Start</strong> to poll the API.</div>';
        }
        if (d.last_error) {
          html += '<div class="error" style="margin-top:0.5rem;">Last error: ' + escapeHtml(d.last_error) + '</div>';
        }
        html += '<hr class="sep" />';
        const ff = (d.last_result && d.last_result.first_free) ? escapeHtml(d.last_result.first_free) : '—';
        html += '<div>First free: ' + ff + '</div>';
        s.innerHTML = html;
      }).catch(() => { document.getElementById('status').textContent = 'Failed to load status'; });
    }
    function start() { fetch('/start', { method: 'POST', credentials: 'same-origin' }).then(() => refresh()); }
    function stop() { fetch('/stop', { method: 'POST', credentials: 'same-origin' }).then(() => refresh()); }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""

SETTINGS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Settings — e-Consul Monitor</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    label { display: block; margin-top: 0.75rem; font-weight: 600; font-size: 0.85rem; }
    input, textarea { width: 100%; padding: 0.4rem; margin-top: 0.2rem; font-size: 0.95rem; }
    textarea { min-height: 2.5rem; }
    .hint { font-size: 0.8rem; color: #555; font-weight: normal; }
    button { margin-top: 1rem; padding: 0.5rem 1.2rem; cursor: pointer; }
    .flash { padding: 0.75rem; border-radius: 6px; margin: 1rem 0; }
    .ok { background: #e8f5e9; }
    .err { background: #ffebee; }
    a { color: #06c; }
    code { font-size: 0.85rem; }
    .token-box { padding: 0.75rem; background: #f0f4f8; border-radius: 6px; margin: 1rem 0; font-size: 0.9rem; }
    .token-box .bad { color: #b00; font-weight: 600; }
    .token-box .ok { color: #060; }
  </style>
</head>
<body>
  <h1>Settings</h1>
  <div class="token-box">
    <strong>Current TOKEN (JWT)</strong><br>
    {% if token_status.exp_utc_iso %}
      Expires (UTC): {{ token_status.exp_utc_iso }}<br>
      {% if token_status.exp_local_display %}Local: {{ token_status.exp_local_display }}<br>{% endif %}
      {% if token_status.expired %}<span class="bad">Expired — paste a new token below.</span>
      {% elif token_status.expires_in_human %}<span class="ok">~{{ token_status.expires_in_human }} remaining</span>{% endif %}
    {% else %}
      <span class="bad">{{ token_status.issues or 'No expiry info' }}</span>
    {% endif %}
  </div>
  {% for c, m in get_flashed_messages(with_categories=true) %}
  <div class="flash {{ 'err' if c == 'error' else 'ok' }}">{{ m }}</div>
  {% endfor %}
  <form method="post" action="/settings">
    <label>TOKEN (JWT) <span class="hint">{% if token_configured %}✓ saved{% else %}not set{% endif %}</span></label>
    <input type="password" name="token" autocomplete="off" placeholder="Paste new token or leave blank to keep">

    <label>USER_AGENT <span class="hint">required</span></label>
    <input type="text" name="user_agent" value="{{ user_agent }}" required>

    <label>INTERVAL (seconds)</label>
    <input type="number" name="interval" min="60" max="86400" value="{{ interval }}">

    <label>OPERATION_NAME <span class="hint">exact service name from schedule</span></label>
    <input type="text" name="operation_name" value="{{ operation_name }}" required>

    <button type="submit">Save to .env</button>
  </form>
  {{ nav_html|safe }}
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in — e-Consul Monitor</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 360px; margin: 3rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    label { display: block; margin-top: 1rem; font-weight: 600; font-size: 0.85rem; }
    input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; font-size: 1rem; }
    button { margin-top: 1.25rem; padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer; width: 100%; }
    .flash { padding: 0.75rem; background: #ffebee; border-radius: 6px; margin: 1rem 0; font-size: 0.9rem; }
    .hint { font-size: 0.8rem; color: #555; margin-top: 1rem; }
  </style>
</head>
<body>
  <h1>e-Consul Monitor</h1>
  {% for c, m in get_flashed_messages(with_categories=true) %}
  <div class="flash">{{ m }}</div>
  {% endfor %}
  <form method="post" action="{{ url_for('login') }}">
    <input type="hidden" name="next" value="{{ next or '' }}">
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input id="password" type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""


@app.before_request
def _require_web_login():
    if not _web_auth_configured():
        return None
    if request.endpoint in ("login", "static") or request.path == "/favicon.ico":
        return None
    if _web_session_ok():
        return None
    if request.accept_mimetypes.best == "application/json" and request.path.startswith("/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _web_auth_configured():
        return redirect(url_for("index"))
    if request.method == "POST":
        if _web_check_credentials(
            (request.form.get("username") or "").strip(),
            (request.form.get("password") or "").strip(),
        ):
            session["web_auth"] = True
            session.permanent = True
            nxt = (request.form.get("next") or request.args.get("next") or "").strip()
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        flash("Invalid username or password.", "error")
    return render_template_string(
        LOGIN_HTML,
        get_flashed_messages=get_flashed_messages,
        next=request.args.get("next", ""),
        url_for=url_for,
    )


@app.route("/logout")
def logout():
    session.pop("web_auth", None)
    if _web_auth_configured():
        return redirect(url_for("login"))
    return redirect(url_for("index"))


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        booking_link=BOOKING_LINK,
        get_flashed_messages=get_flashed_messages,
        nav_html=nav_html(),
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        ok, msg = save_settings_from_form(
            token=request.form.get("token"),
            user_agent=request.form.get("user_agent", ""),
            interval=request.form.get("interval", "300"),
            operation_name=request.form.get("operation_name", ""),
        )
        if ok:
            flash(msg, "success")
        else:
            flash(msg, "error")
        return redirect(url_for("settings"))

    s = get_settings_for_form()
    return render_template_string(
        SETTINGS_HTML,
        get_flashed_messages=get_flashed_messages,
        env_path=s["env_path"],
        token_configured=s["token_configured"],
        user_agent=s["user_agent"],
        interval=s["interval"],
        operation_name=s["operation_name"],
        token_status=s["token_status"],
        nav_html=nav_html(),
    )


@app.route("/status")
def status():
    return jsonify(get_monitor().get_status())


@app.route("/start", methods=["POST"])
def start():
    print(f"[e-consul] HTTP POST /start pid={os.getpid()}", flush=True)
    ok = get_monitor().start()
    print(f"[e-consul] HTTP POST /start result started={ok}", flush=True)
    return jsonify({"ok": ok, "message": "Started" if ok else "Already running"})


@app.route("/stop", methods=["POST"])
def stop():
    print(f"[e-consul] HTTP POST /stop pid={os.getpid()}", flush=True)
    ok = get_monitor().stop()
    print(f"[e-consul] HTTP POST /stop result stopped={ok}", flush=True)
    return jsonify({"ok": ok, "message": "Stopped" if ok else "Not running"})


if __name__ == "__main__":
    from monitor import reload_config
    reload_config()
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
