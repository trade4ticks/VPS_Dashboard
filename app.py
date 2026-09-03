import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()  # must run before importing config so os.environ.get() picks up .env values

import config

app = FastAPI(title="VPS Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_service(service: str) -> str:
    """Returns 'active', 'inactive', 'failed', or 'unknown'."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def run_command(cmd: list, cwd: str = None, timeout: int = 60) -> dict:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        return {"success": result.returncode == 0, "output": output}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "output": str(e)}


def format_bytes(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def relative_age(dt: datetime) -> str:
    """Human 'X ago' string from a datetime in server-local time."""
    if not dt:
        return None
    secs = int((datetime.now() - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 90:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 90:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 36:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"


def _read_tail(path: str, lines: int):
    """Return the last `lines` of a file as a list, or None if unreadable."""
    result = run_command(["tail", "-n", str(lines), path], timeout=10)
    if not result["success"]:
        return None
    return result["output"].splitlines()


def _simple_cron_status(lines: list, st: dict) -> dict:
    """Most-recent-marker-wins status for single-run jobs (fetch_trades, spx)."""
    ts_re = re.compile(st["ts_regex"])
    succ_re = re.compile(st["success_regex"])
    fail_re = re.compile(st["failure_regex"]) if st.get("failure_regex") else None
    warn_re = re.compile(st["warn_regex"]) if st.get("warn_regex") else None
    fmt = st["ts_format"]
    # Some logs (the Open_Interest ones) print their summary markers without a
    # per-line timestamp; the run's time appears only once, in its
    # "Log: .../..._YYYYMMDD_HHMMSS.log" header. With ts_carry the most recent
    # timestamp seen is attributed to later markers, so every marker in a run
    # block shares that run's timestamp -- which is also how a warning is
    # matched to the success it belongs to.
    carry = st.get("ts_carry", False)

    def _ts(line):
        m = ts_re.search(line)
        if not m:
            return None
        try:
            return m.group(1), datetime.strptime(m.group(1), fmt)
        except ValueError:
            return m.group(1), None

    last_succ = last_fail = last_warn = None  # (raw, dt)
    running = None                            # most recent timestamp seen
    for line in lines:
        found = _ts(line)
        if found:
            running = found
        cur = found or (running if carry else None)
        if succ_re.search(line):
            last_succ = cur or last_succ
        if fail_re and fail_re.search(line):
            last_fail = cur or last_fail
        if warn_re and warn_re.search(line):
            last_warn = cur or last_warn

    def _dt(pair):
        return pair[1] if pair and pair[1] else datetime.min

    if last_succ and last_fail:
        win, status = (last_succ, "success") if _dt(last_succ) >= _dt(last_fail) else (last_fail, "failed")
    elif last_succ:
        win, status = last_succ, "success"
    elif last_fail:
        win, status = last_fail, "failed"
    else:
        win, status = None, "unknown"

    # A warning marker from the same run as the winning success (identical
    # carried timestamp) downgrades success -> warning rather than failure.
    if status == "success" and last_warn and win and last_warn[0] == win[0]:
        status = "warning"

    when = None
    if win:
        disp = st.get("display_format")
        when = win[1].strftime(disp) if disp and win[1] else win[0]

    return {"label": st.get("label", "Run"), "status": status, "when": when}


def _read_grep(path: str, pattern: str, limit: int):
    """Return marker lines matching `pattern` (extended, case-insensitive),
    keeping the last `limit` matches. Used for the OI log, whose runs are too
    large to tail — but the run header/outcome markers are sparse."""
    result = run_command(["grep", "-iE", pattern, path], timeout=15)
    if not result["success"]:  # grep exits non-zero when there are no matches
        return []
    lines = result["output"].splitlines()
    return lines[-limit:] if limit else lines


def _tiered_cron_status(lines: list, st: dict) -> list:
    """Per-tier status for the OI pipeline (tiers share one log). `lines` are
    the grepped marker lines in file order: each run header (with tier + date)
    is followed later by its completion or abort line."""
    start_re = re.compile(st["start_regex"])
    succ_re = re.compile(st["success_regex"])
    fail_re = re.compile(st["failure_regex"]) if st.get("failure_regex") else None
    warn_re = re.compile(st["warn_regex"]) if st.get("warn_regex") else None
    tier_re = re.compile(st["tier_regex"]) if st.get("tier_regex") else None
    pre_re = re.compile(st["premarket_regex"]) if st.get("premarket_regex") else None
    date_re = re.compile(st["date_in_start_regex"]) if st.get("date_in_start_regex") else None
    time_re = re.compile(st["time_regex"])

    def _time(line):
        m = time_re.search(line)
        return m.group(1) if m else None

    by_label, cur = {}, None
    for line in lines:
        if start_re.search(line):  # new run header
            if pre_re and pre_re.search(line):
                label = "PREMARKET"
            elif tier_re and tier_re.search(line):
                label = tier_re.search(line).group(1)
            else:
                label = "Run"
            date_str = date_re.search(line).group(1) if (date_re and date_re.search(line)) else None
            cur = {"label": label, "status": "running", "date": date_str, "time": _time(line)}
            by_label[label] = cur  # latest run for this tier wins
        elif cur is not None:
            if succ_re.search(line):
                cur["status"] = "warning" if (warn_re and warn_re.search(line)) else "success"
                cur["time"] = _time(line) or cur["time"]
            elif fail_re and fail_re.search(line):
                cur["status"] = "failed"
                cur["time"] = _time(line) or cur["time"]

    def _sortkey(date_str, time_str):
        try:
            if date_str and time_str:
                return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
            if time_str:
                return datetime.strptime(time_str, "%H:%M:%S")
        except ValueError:
            pass
        return datetime.min

    runs = []
    for r in by_label.values():
        when = " ".join(p for p in (r["date"], r["time"]) if p) or None
        runs.append({"label": r["label"], "status": r["status"], "when": when,
                     "_dt": _sortkey(r["date"], r["time"])})

    # Drop runs whose tier couldn't be resolved (e.g. an older log format that
    # logged no "tier =" field); only show the configured tiers.
    order = st.get("tiers")
    if order:
        runs = [r for r in runs if r["label"] in order]

    runs.sort(key=lambda r: r["_dt"])  # chronological by timestamp
    for r in runs:
        r.pop("_dt", None)
    return runs


def cron_status_for(info: dict) -> dict:
    """Build the Overview status payload for one cron log entry."""
    st = info["status"]
    path = info.get("path")
    out = {"name": info["name"], "schedule": info.get("schedule"), "time_note": st.get("time_note")}

    if not path or not os.path.exists(path):
        out.update({"exists": False, "runs": []})
        return out

    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    out.update({"exists": True, "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"), "ago": relative_age(mtime)})

    if st.get("tiered"):
        lines = _read_grep(path, st["grep_regex"], st.get("marker_lines", 400))
        out["runs"] = _tiered_cron_status(lines, st)
    else:
        lines = _read_tail(path, st.get("lines", 200)) or []
        out["runs"] = [_simple_cron_status(lines, st)]
    return out


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/")
def overview(request: Request):
    return templates.TemplateResponse(request, "overview.html", {"active": "overview"})


@app.get("/services")
def services(request: Request):
    return templates.TemplateResponse(request, "services.html", {
        "active": "services",
        "services": config.SERVICES,
        "trading": config.TRADING_CONTROL,
        "projects": config.PROJECTS,
    })


@app.get("/files")
def files(request: Request):
    return templates.TemplateResponse(request, "files.html", {
        "active": "files",
        "projects": config.PROJECTS,
        "browse_paths": config.BROWSE_PATHS,
    })


@app.get("/logs")
def logs(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "active": "logs",
        "log_files": config.LOG_FILES,
    })


@app.get("/disk")
def disk(request: Request):
    return templates.TemplateResponse(request, "disk.html", {"active": "disk"})


@app.get("/cheatsheet")
def cheatsheet(request: Request):
    return templates.TemplateResponse(request, "cheatsheet.html", {"active": "cheatsheet"})


@app.get("/data-inspector")
def data_inspector(request: Request):
    return templates.TemplateResponse(request, "data_inspector.html", {
        "active": "data_inspector",
        "parquet_sources": config.PARQUET_SOURCES,
    })


# ---------------------------------------------------------------------------
# API — status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    results = {}
    for key, svc in config.SERVICES.items():
        port_ok = None
        if svc.get("port"):
            port_ok = check_port(svc.get("host", "localhost"), svc["port"])
        service_active = None
        if svc.get("service"):
            service_active = check_service(svc["service"])
        results[key] = {
            "name": svc["name"],
            "port": svc.get("port"),
            "port_reachable": port_ok,
            "service_active": service_active,
        }
    return results


# ---------------------------------------------------------------------------
# API — actions
# ---------------------------------------------------------------------------

@app.post("/api/restart/{service_key}")
def api_restart(service_key: str):
    if service_key not in config.SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_key}")
    svc = config.SERVICES[service_key]
    if not svc.get("service"):
        raise HTTPException(status_code=400, detail="No systemd unit configured for this service")
    result = run_command(["systemctl", "restart", svc["service"]])
    if result["success"] and not result["output"]:
        result["output"] = f"{svc['service']} restarted successfully."
    return result


@app.post("/api/git-pull/{project_key}")
def api_git_pull(project_key: str):
    if project_key not in config.PROJECTS:
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_key}")
    project = config.PROJECTS[project_key]
    return run_command(["git", "pull"], cwd=project["path"])


def project_units(project: dict) -> list:
    """Systemd units to restart for a project, in order. A project may declare a
    single "service" (str) or several "services" (list) -- /spx_analysis_dashboard
    is one repo serving both spx-dashboard and spx-live."""
    units = project.get("services")
    if units:
        return [u for u in units if u]
    unit = project.get("service")
    return [unit] if unit else []


@app.post("/api/deploy/{project_key}")
def api_deploy(project_key: str):
    if project_key not in config.PROJECTS:
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_key}")
    project = config.PROJECTS[project_key]
    units = project_units(project)
    if not units:
        raise HTTPException(status_code=400, detail="No systemd unit configured for this project")

    pull = run_command(["git", "pull"], cwd=project["path"])
    output = f"=== git pull ===\n{pull['output']}\n"
    if not pull["success"]:
        return {"success": False, "output": output}

    # Restart every unit even if an earlier one fails: they share a working tree
    # that has already been updated, so stopping half-way would leave the
    # remaining unit running code that no longer matches the repo.
    ok = True
    for unit in units:
        restart = run_command(["systemctl", "restart", unit])
        output += f"\n=== systemctl restart {unit} ===\n"
        output += (restart["output"] if restart["output"] else "Restarted successfully.") + "\n"
        ok = ok and restart["success"]
    return {"success": ok, "output": output}


# ---------------------------------------------------------------------------
# API — live trading arm/disarm (proxied to the spx-live service)
# ---------------------------------------------------------------------------
#
# The live service's contract (spx_analysis_dashboard live/main.py, broker.py):
#
#   GET  /broker/trading  -> the state object, flat.
#   POST /broker/trading  {"enabled": bool, "who": str} + X-Live-Token header
#                         -> {"ok": bool, "why": str, "trading": {state}}
#
# Three things about it drive the code below:
#
#   * A REFUSAL IS HTTP 200 with ok=false, not a 4xx. Status codes say nothing
#     about whether the flip happened; only the "ok" field does.
#   * The state is NESTED under "trading" on POST and flat on GET.
#   * env_enabled, runtime_enabled and can_enable are three separate things.
#     can_enable mirrors the environment gate ONLY -- it can be true while
#     arming still fails because LIVE_CONTROL_TOKEN is unset, so the token is
#     checked separately before offering the button.

def _trading_call(method: str, payload: dict = None, token: str = None) -> dict:
    """Call the live service's /broker/trading endpoint.

    Returns transport-level outcome only: ok=True means we got a response and
    parsed it, NOT that the service agreed to do anything.
    """
    cfg = config.TRADING_CONTROL
    url = cfg["base_url"].rstrip("/") + cfg.get("path", "/broker/trading")
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        # The header is the right place for it service-to-service; it is never
        # logged or echoed back to the browser.
        headers["X-Live-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 5)) as resp:
            return {"ok": True, "status": resp.status, "body": json.loads(resp.read() or b"{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"why": raw.decode(errors="replace").strip()}
        return {"ok": False, "status": exc.code,
                "body": body if body else {"why": f"HTTP {exc.code}"}}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": None,
                "body": {"why": f"live service unreachable: {exc.reason}"}}
    except (ValueError, OSError) as exc:
        return {"ok": False, "status": None,
                "body": {"why": f"bad response from live service: {exc}"}}


def _trading_state(st: dict) -> dict:
    """Normalise the live service's state, adding what the UI needs to decide.

    can_arm is deliberately stricter than the service's can_enable: enabling
    also requires LIVE_CONTROL_TOKEN to be set, which can_enable does not
    reflect, and offering a button that is certain to be refused is worse than
    showing why it is unavailable.
    """
    env_on = st.get("env_enabled")
    token_set = st.get("control_token_set")
    changed_at = st.get("changed_at")
    when = None
    if isinstance(changed_at, (int, float)):
        when = datetime.fromtimestamp(changed_at).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "allowed": st.get("allowed"),
        "env_enabled": env_on,
        "runtime_enabled": st.get("runtime_enabled"),
        "can_enable": st.get("can_enable"),
        "control_token_set": token_set,
        "can_arm": bool(st.get("can_enable")) and bool(token_set),
        "changed_at": when,
        "changed_by": st.get("changed_by"),
        "why": st.get("why"),
    }


def _control_token(supplied: str = None) -> Optional[str]:
    """The token to send when arming: whatever the operator typed, else this
    dashboard's own environment if it has been configured with one.

    The value itself is never returned to the browser or written to a log --
    callers only ever learn whether one exists.
    """
    if supplied:
        return supplied
    name = config.TRADING_CONTROL.get("token_env")
    return (os.environ.get(name, "").strip() or None) if name else None


@app.get("/api/trading")
def api_trading_get():
    """Current state of the live trading gates."""
    res = _trading_call("GET")
    if not res["ok"]:
        return {"available": False, "why": res["body"].get("why") or f"HTTP {res['status']}",
                "env_enabled": None, "runtime_enabled": None, "can_arm": False,
                "token_available": False}
    # A bool, never the secret: it only tells the UI whether to prompt.
    return {"available": True, "token_available": bool(_control_token()),
            **_trading_state(res["body"])}


class TradingSetBody(BaseModel):
    enabled: bool
    token: Optional[str] = None


@app.post("/api/trading")
def api_trading_set(body: TradingSetBody):
    """Arm or disarm live trading.

    The token is forwarded, never stored. Disabling is never refused by the
    live service and deliberately does not send one.
    """
    verb = "Arm" if body.enabled else "Disarm"
    res = _trading_call(
        "POST",
        {"enabled": body.enabled, "who": config.TRADING_CONTROL.get("who", "vps-dashboard")},
        # Disabling is never refused and deliberately carries no token.
        token=_control_token(body.token) if body.enabled else None,
    )

    if not res["ok"]:
        why = res["body"].get("why") or f"HTTP {res['status']}"
        return {"success": False, "available": False,
                "output": f"{verb} failed: could not reach the live service.\n\n{why}",
                "can_arm": False}

    envelope = res["body"]
    state = _trading_state(envelope.get("trading") or {})

    # HTTP 200 with ok=false is the service refusing; "why" explains it.
    if envelope.get("ok") is not True:
        why = envelope.get("why") or "the live service refused without a reason."
        return {"success": False, "available": True,
                "output": f"{verb} refused by the live service.\n\n{why}", **state}

    if state.get("runtime_enabled"):
        summary = ("LIVE — the environment gate is open and the runtime flag is set, "
                   "so orders can leave.")
    else:
        summary = "Disarmed — the runtime flag is off, so no order can leave."
    return {"success": True, "available": True,
            "output": f"{verb} accepted.\n\n{summary}", **state}


@app.post("/api/force-run/{log_key}")
def api_force_run(log_key: str, run: str = Query(None)):
    """Manually trigger a cron job's command. Launches detached; the command
    redirects its own output to the log file, so progress shows on the Logs page."""
    info = config.LOG_FILES.get(log_key)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unknown log: {log_key}")

    runs = info.get("runs")
    if not runs:
        raise HTTPException(status_code=400, detail="This log has no runnable command")

    # Pick the requested run by label, else the first.
    if run:
        target = next((r for r in runs if r["label"] == run), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"Unknown run: {run}")
    else:
        target = runs[0]

    label = target["label"]
    command = target["command"]
    lock = target.get("lock")

    # Concurrency guard: if the lock is held (by cron or a prior manual run),
    # don't launch a second copy. flock -n exits non-zero when it can't acquire.
    if lock:
        check = run_command(["flock", "-n", lock, "-c", "true"], timeout=10)
        if not check["success"]:
            return {
                "success": False,
                "output": f"'{label}' looks like it's already running (lock {lock} held). Try again shortly.",
            }

    # Launch detached so a long-running pipeline doesn't block the request.
    # The command itself appends to the log file via '>> ... 2>&1'.
    try:
        subprocess.Popen(
            ["bash", "-c", command],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"success": False, "output": f"Failed to launch: {e}"}

    return {"success": True, "output": f"Started '{label}'. Watch the log below for progress."}


def _mtime_only_status(info: dict) -> dict:
    """Fallback payload for a job with no "status" block: we can't classify the
    run without knowing the log's line format, but the log's last-write time is
    still when the job last produced output."""
    path = info.get("path")
    label = info.get("overview_label") or info["name"].replace(" (cron)", "")
    out = {"name": info["name"], "schedule": info.get("schedule"), "time_note": None}

    if not path or not os.path.exists(path):
        out.update({"exists": False, "runs": []})
        return out

    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    out.update({
        "exists": True,
        "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "ago": relative_age(mtime),
        "runs": [{
            "label": label,
            "status": "unknown",
            "when": mtime.strftime("%Y-%m-%d %H:%M"),
        }],
    })
    return out


def _overview_card(card: dict) -> dict:
    """Merge one or more LOG_FILES entries into a single Overview card.

    Run rows are concatenated in member order. Because members can timestamp
    differently (the OI tiers log bare ET times, others log full datetimes),
    a per-member time_note is folded into its own rows unless every member
    agrees on the note -- that keeps the card-level note when there's only one.
    """
    parts = []
    for key in card["members"]:
        info = config.LOG_FILES.get(key)
        if not info:
            continue
        part = cron_status_for(info) if info.get("status") else _mtime_only_status(info)
        # A member whose log is missing yields no rows; on a shared card that
        # would make the job disappear silently, so stand in a placeholder row
        # (one per tier for tiered jobs) that renders with an em dash.
        if not part.get("runs"):
            st = info.get("status") or {}
            labels = st.get("tiers") if st.get("tiered") else [
                st.get("label") or info.get("overview_label")
                or info["name"].replace(" (cron)", "")
            ]
            part["runs"] = [{"label": l, "status": "unknown", "when": None} for l in labels]
        parts.append(part)

    if not parts:
        return None

    notes = {p.get("time_note") for p in parts if p.get("runs")}
    uniform_note = notes.pop() if len(notes) == 1 else None
    if uniform_note is None:
        for p in parts:
            note = p.get("time_note")
            if note:
                for r in p.get("runs", []):
                    if r.get("when"):
                        r["when"] = f"{r['when']} {note}"

    runs = [r for p in parts for r in p.get("runs", [])]
    existing = [p for p in parts if p.get("exists")]
    newest = max((p["mtime"] for p in existing), default=None)

    return {
        "name": card.get("name") or parts[0]["name"],
        "schedule": parts[0].get("schedule") if len(parts) == 1 else None,
        "time_note": uniform_note,
        "exists": bool(existing),
        "mtime": newest,
        "ago": relative_age(datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")) if newest else None,
        "runs": runs,
    }


@app.get("/api/cron-status")
def api_cron_status():
    """Last-run status of the top-level cron jobs, for the Overview page.
    Cards are composed per config.CRON_OVERVIEW_CARDS; rows come from each
    member's log markers (see config status blocks)."""
    results = {}
    for i, card in enumerate(config.CRON_OVERVIEW_CARDS):
        payload = _overview_card(card)
        if payload:
            results[card.get("name") or card["members"][0] or str(i)] = payload
    return results


# ---------------------------------------------------------------------------
# API — file browser (lazy: returns immediate children only)
# ---------------------------------------------------------------------------

# Virtual/kernel filesystems — never descend into these
SKIP_ROOTS = {"/proc", "/sys", "/dev", "/run", "/snap"}
# Noisy dirs to hide everywhere
SKIP_NAMES = {".venv", "__pycache__", ".git", "node_modules", ".idea",
              "diskcache", ".dash_cache", ".tasty_sessions"}


@app.get("/api/browse")
def api_browse(path: str = "/"):
    real = os.path.realpath(path)

    for skip in SKIP_ROOTS:
        if real == skip or real.startswith(skip + "/"):
            return []

    if not os.path.isdir(real):
        raise HTTPException(status_code=400, detail="Not a directory")

    items = []
    try:
        entries = sorted(os.scandir(real), key=lambda e: (e.is_file(), e.name.lower()))
        for entry in entries:
            if entry.name in SKIP_NAMES:
                continue
            # Hide virtual kernel filesystems from the listing entirely
            if entry.path in SKIP_ROOTS:
                continue
            # Hide dot-files except at top-level roots like /root
            if entry.name.startswith(".") and real not in ("/", "/root"):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child_count = sum(1 for _ in os.scandir(entry.path))
                    except PermissionError:
                        child_count = None
                    items.append({
                        "name": entry.name,
                        "type": "dir",
                        "path": entry.path,
                        "items": child_count,
                    })
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat()
                    items.append({
                        "name": entry.name,
                        "type": "file",
                        "path": entry.path,
                        "size": format_bytes(stat.st_size),
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
            except (PermissionError, OSError):
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied reading " + real)

    return items


@app.get("/api/du")
def api_du(paths: list[str] = Query(default=[])):
    """Return disk usage for a list of paths. Called after a directory expands."""
    if not paths:
        return {}
    # Strip out virtual filesystems — du produces nonsense sizes for /proc etc.
    safe_paths = [p for p in paths if not any(
        os.path.realpath(p) == s or os.path.realpath(p).startswith(s + "/")
        for s in SKIP_ROOTS
    )]
    if not safe_paths:
        return {}
    result = subprocess.run(
        ["du", "-sb", "--"] + safe_paths,
        capture_output=True, text=True, timeout=60,
    )
    sizes = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                sizes[parts[1]] = format_bytes(int(parts[0]))
            except ValueError:
                pass
    return sizes


# ---------------------------------------------------------------------------
# API — logs
# ---------------------------------------------------------------------------

@app.get("/api/logs/{log_key}")
def api_logs(log_key: str, lines: int = 200):
    if log_key not in config.LOG_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown log: {log_key}")
    info = config.LOG_FILES[log_key]

    # --- Postgres log type ---
    if info.get("type") == "postgres":
        return _fetch_postgres_log(info, lines)

    # --- File log type (default) ---
    path = info["path"]

    if not os.path.exists(path):
        return {
            "path": path, "exists": False,
            "content": "(Log file does not exist yet)",
            "size": None, "modified": None,
        }

    result = subprocess.run(
        ["tail", "-n", str(lines), path],
        capture_output=True, text=True, timeout=10,
    )
    stat = os.stat(path)
    return {
        "path": path,
        "exists": True,
        "content": result.stdout,
        "size": format_bytes(stat.st_size),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _format_ts(val) -> str:
    if hasattr(val, 'strftime'):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
    return str(val)


def _fetch_postgres_log(info: dict, limit: int) -> dict:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return {"path": "postgres", "exists": False,
                "content": "(DATABASE_URL not configured in .env)",
                "size": None, "modified": None}

    table = info["table"]
    # Whitelist table name to prevent injection
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table):
        return {"path": "postgres", "exists": False,
                "content": "(Invalid table name)", "size": None, "modified": None}

    try:
        con = psycopg2.connect(db_url)
        cur = con.cursor()

        # Get column names
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position", (table,)
        )
        columns = [r[0] for r in cur.fetchall()]
        if not columns:
            con.close()
            return {"path": f"postgres:{table}", "exists": False,
                    "content": f"(Table '{table}' not found)", "size": None, "modified": None}

        # Fetch recent rows (newest first)
        cur.execute(f"SELECT * FROM {table} ORDER BY ts DESC LIMIT %s", (limit,))
        rows = cur.fetchall()

        # Get total row count
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        total = cur.fetchone()[0]

        con.close()

        # Format as readable text lines
        output_lines = []
        col_header = " | ".join(f"{c:>20}" for c in columns)
        output_lines.append(col_header)
        output_lines.append("-" * len(col_header))
        for row in rows:
            output_lines.append(" | ".join(f"{str(v or ''):>20}" for v in row))

        return {
            "path": f"postgres:{table}",
            "exists": True,
            "content": "\n".join(output_lines),
            "size": f"{total} rows total",
            "modified": _format_ts(rows[0][0]) if rows and rows[0][0] else None,
        }
    except Exception as exc:
        return {"path": f"postgres:{table}", "exists": True,
                "content": f"(Database error: {exc})", "size": None, "modified": None}


# ---------------------------------------------------------------------------
# API — disk
# ---------------------------------------------------------------------------

def get_postgres_sizes() -> dict:
    """Return per-database and per-table sizes from PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=config.POSTGRES_CONN["host"],
            port=config.POSTGRES_CONN["port"],
            user=config.POSTGRES_CONN["user"],
            password=config.POSTGRES_CONN["password"],
            database=config.POSTGRES_CONN["user"],
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT datname, pg_database_size(datname)
            FROM pg_database
            WHERE datname NOT IN ('template0', 'template1', 'postgres')
            ORDER BY pg_database_size(datname) DESC
        """)
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        databases = []
        for datname, db_size_bytes in db_rows:
            tables = []
            try:
                db_conn = psycopg2.connect(
                    host=config.POSTGRES_CONN["host"],
                    port=config.POSTGRES_CONN["port"],
                    user=config.POSTGRES_CONN["user"],
                    password=config.POSTGRES_CONN["password"],
                    database=datname,
                    connect_timeout=5,
                )
                db_cur = db_conn.cursor()
                db_cur.execute("""
                    SELECT schemaname || '.' || tablename,
                           pg_total_relation_size(
                               quote_ident(schemaname) || '.' || quote_ident(tablename)
                           )
                    FROM pg_tables
                    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                    ORDER BY 2 DESC NULLS LAST
                """)
                tables = [
                    {"name": row[0], "size_bytes": row[1] or 0, "size": format_bytes(row[1] or 0)}
                    for row in db_cur.fetchall()
                ]
                db_cur.close()
                db_conn.close()
            except Exception:
                pass

            databases.append({
                "name": datname,
                "size_bytes": db_size_bytes,
                "size": format_bytes(db_size_bytes),
                "tables": tables,
            })

        return {"available": True, "databases": databases}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


@app.get("/api/disk")
def api_disk():
    sections = []
    for label, path in config.DISK_PATHS.items():
        if not os.path.exists(path):
            sections.append({"label": label, "path": path, "exists": False})
            continue

        subdirs = []
        total_size = 0
        total_files = 0
        for entry in sorted(Path(path).iterdir()):
            if entry.is_dir():
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                count = sum(1 for f in entry.rglob("*") if f.is_file())
                subdirs.append({
                    "name": entry.name,
                    "size": format_bytes(size),
                    "size_bytes": size,
                    "files": count,
                })
                total_size += size
                total_files += count

        sections.append({
            "label": label, "path": path, "exists": True,
            "subdirs": subdirs,
            "total_size": format_bytes(total_size),
            "total_files": total_files,
        })

    # Collect df info for each configured volume
    volumes = []
    for label, mount in config.DISK_VOLUMES.items():
        result = subprocess.run(
            ["df", "-h", mount], capture_output=True, text=True,
        )
        if result.returncode != 0:
            volumes.append({"label": label, "mount": mount, "available": False})
            continue
        # Parse second line: device  size  used  avail  use%  mount
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            volumes.append({
                "label": label,
                "mount": mount,
                "available": True,
                "device": parts[0] if len(parts) > 0 else "",
                "size": parts[1] if len(parts) > 1 else "",
                "used": parts[2] if len(parts) > 2 else "",
                "avail": parts[3] if len(parts) > 3 else "",
                "use_pct": parts[4] if len(parts) > 4 else "",
            })
        else:
            volumes.append({"label": label, "mount": mount, "available": False})

    return {"sections": sections, "volumes": volumes, "postgres": get_postgres_sizes()}


# ---------------------------------------------------------------------------
# API — crontab
# ---------------------------------------------------------------------------

@app.get("/api/crontab")
def api_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    entries = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return {"entries": entries}


# ---------------------------------------------------------------------------
# API — Parquet / Data Inspector
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{8}$")
_SAFE_NAME_RE = re.compile(r"^[\w\-\.]+$")

# Default source key (first entry in PARQUET_SOURCES)
_DEFAULT_SOURCE = next(iter(config.PARQUET_SOURCES))


def _resolve_base(source: str | None) -> str:
    """Return the base path for the given source key."""
    key = source or _DEFAULT_SOURCE
    entry = config.PARQUET_SOURCES.get(key)
    if not entry:
        raise HTTPException(status_code=400, detail=f"Unknown parquet source: {key}")
    return entry["path"]


def _require_date(date: str) -> None:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="Invalid date — use YYYYMMDD (e.g. 20250722)")


def _date_path(date: str, source: str | None = None) -> Path:
    """Return Path for the date folder and verify it stays within the source base."""
    base_str = _resolve_base(source)
    candidate = (Path(base_str) / date).resolve()
    base = Path(base_str).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    return candidate


def _parquet_glob(date: str, expiration: str | None, source: str | None = None) -> str:
    base = _resolve_base(source)
    if expiration:
        return f"{base}/{date}/{expiration}/*.parquet"
    return f"{base}/{date}/**/*.parquet"


@app.get("/api/parquet/inspect")
def api_parquet_inspect(date: str, source: str = None):
    _require_date(date)
    folder = _date_path(date, source)

    if not folder.exists():
        return {"exists": False, "date": date, "path": str(folder)}

    files, subfolders, total_size = [], [], 0

    for entry in sorted(folder.iterdir()):
        try:
            if entry.is_dir():
                try:
                    size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                    count = sum(1 for f in entry.rglob("*") if f.is_file())
                except (PermissionError, OSError):
                    size, count = 0, 0
                subfolders.append({"name": entry.name, "size": format_bytes(size), "files": count})
                total_size += size
            elif entry.is_file() and entry.suffix == ".parquet":
                stat = entry.stat()
                files.append({"name": entry.name, "size": format_bytes(stat.st_size)})
                total_size += stat.st_size
        except (PermissionError, OSError):
            continue

    return {
        "exists": True,
        "date": date,
        "path": str(folder),
        "files": files,
        "subfolders": subfolders,
        "total_size": format_bytes(total_size),
        "total_file_count": len(files) + sum(s["files"] for s in subfolders),
    }


@app.get("/api/parquet/expirations")
def api_parquet_expirations(date: str, source: str = None):
    _require_date(date)
    folder = _date_path(date, source)

    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Date folder not found: {folder}")

    expirations = sorted(e.name for e in folder.iterdir() if e.is_dir())
    return {"date": date, "expirations": expirations}


@app.get("/api/parquet/schema")
def api_parquet_schema(date: str, expiration: str = None, source: str = None):
    _require_date(date)
    _date_path(date, source)  # validate path

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    glob = _parquet_glob(date, expiration, source)
    try:
        con = duckdb.connect()
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 0"
        ).fetchall()
        con.close()
        return {
            "date": date,
            "expiration": expiration,
            "columns": [{"name": r[0], "type": r[1]} for r in rows],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DuckDB: {exc}")


@app.get("/api/parquet/row-counts")
def api_parquet_row_counts(date: str, expiration: str = None, source: str = None):
    _require_date(date)
    folder = _date_path(date, source)

    if not folder.exists():
        raise HTTPException(status_code=404, detail="Date folder not found")

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    try:
        con = duckdb.connect()
        results = []

        if expiration:
            glob = _parquet_glob(date, expiration, source)
            count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
            results.append({"expiration": expiration, "rows": count})
        else:
            subfolders = sorted(e.name for e in folder.iterdir() if e.is_dir())
            if subfolders:
                for exp in subfolders:
                    try:
                        glob = _parquet_glob(date, exp, source)
                        count = con.execute(
                            f"SELECT COUNT(*) FROM read_parquet('{glob}')"
                        ).fetchone()[0]
                        results.append({"expiration": exp, "rows": count})
                    except Exception:
                        results.append({"expiration": exp, "rows": None})
            else:
                base = _resolve_base(source)
                glob = f"{base}/{date}/*.parquet"
                count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
                results.append({"expiration": "(all)", "rows": count})

        con.close()
        total = sum(r["rows"] for r in results if r["rows"] is not None)
        return {"date": date, "expiration": expiration, "rows": results, "total": total}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DuckDB: {exc}")


@app.get("/api/parquet/preview")
def api_parquet_preview(date: str, expiration: str = None, limit: int = 50, source: str = None):
    _require_date(date)
    _date_path(date, source)  # validate path

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    limit = min(max(1, limit), 200)
    glob = _parquet_glob(date, expiration, source)

    try:
        con = duckdb.connect()
        col_rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 0"
        ).fetchall()
        columns = [r[0] for r in col_rows]
        data = con.execute(
            f"SELECT * FROM read_parquet('{glob}') LIMIT {limit}"
        ).fetchall()
        con.close()
        return {
            "date": date,
            "expiration": expiration,
            "columns": columns,
            "rows": [list(r) for r in data],
            "count": len(data),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DuckDB: {exc}")


_SQL_BLOCK_RE = re.compile(
    r'\b(insert|update|delete|drop|create|alter|truncate|copy|export|attach|detach|load|install)\b',
    re.IGNORECASE,
)


class SqlBody(BaseModel):
    sql: str


@app.post("/api/parquet/query")
def api_parquet_query(body: SqlBody):
    sql = body.sql.strip()
    if not sql.upper().startswith("SELECT"):
        raise HTTPException(status_code=400, detail="Only SELECT statements are allowed")
    if _SQL_BLOCK_RE.search(sql):
        raise HTTPException(status_code=400, detail="Query contains a disallowed keyword")

    # Wrap in subquery to enforce hard row cap
    wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT 200"

    try:
        con = duckdb.connect()
        col_rows = con.execute(f"DESCRIBE {wrapped}").fetchall()
        columns = [r[0] for r in col_rows]
        data = con.execute(wrapped).fetchall()
        con.close()
        return {
            "columns": columns,
            "rows": [list(r) for r in data],
            "count": len(data),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DuckDB: {exc}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, reload=False)
