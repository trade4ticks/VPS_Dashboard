import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
    return templates.TemplateResponse(request, "data_inspector.html", {"active": "data_inspector"})


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


@app.post("/api/deploy/{project_key}")
def api_deploy(project_key: str):
    if project_key not in config.PROJECTS:
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_key}")
    project = config.PROJECTS[project_key]
    if not project.get("service"):
        raise HTTPException(status_code=400, detail="No systemd unit configured for this project")

    pull = run_command(["git", "pull"], cwd=project["path"])
    output = f"=== git pull ===\n{pull['output']}\n"
    if not pull["success"]:
        return {"success": False, "output": output}

    restart = run_command(["systemctl", "restart", project["service"]])
    output += f"\n=== systemctl restart {project['service']} ===\n"
    output += restart["output"] if restart["output"] else "Restarted successfully."
    return {"success": restart["success"], "output": output}


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


# ---------------------------------------------------------------------------
# API — disk
# ---------------------------------------------------------------------------

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

    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
    return {"sections": sections, "df": df.stdout}


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

PARQUET_BASE = "/data/spx_options"
_DATE_RE = re.compile(r"^\d{8}$")
_SAFE_NAME_RE = re.compile(r"^[\w\-\.]+$")


def _require_date(date: str) -> None:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="Invalid date — use YYYYMMDD (e.g. 20250722)")


def _date_path(date: str) -> Path:
    """Return Path for the date folder and verify it stays within PARQUET_BASE."""
    candidate = (Path(PARQUET_BASE) / date).resolve()
    base = Path(PARQUET_BASE).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    return candidate


def _parquet_glob(date: str, expiration: str | None) -> str:
    if expiration:
        return f"{PARQUET_BASE}/{date}/{expiration}/*.parquet"
    return f"{PARQUET_BASE}/{date}/**/*.parquet"


@app.get("/api/parquet/inspect")
def api_parquet_inspect(date: str):
    _require_date(date)
    folder = _date_path(date)

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
def api_parquet_expirations(date: str):
    _require_date(date)
    folder = _date_path(date)

    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Date folder not found: {folder}")

    expirations = sorted(e.name for e in folder.iterdir() if e.is_dir())
    return {"date": date, "expirations": expirations}


@app.get("/api/parquet/schema")
def api_parquet_schema(date: str, expiration: str = None):
    _require_date(date)
    _date_path(date)  # validate path

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    glob = _parquet_glob(date, expiration)
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
def api_parquet_row_counts(date: str, expiration: str = None):
    _require_date(date)
    folder = _date_path(date)

    if not folder.exists():
        raise HTTPException(status_code=404, detail="Date folder not found")

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    try:
        con = duckdb.connect()
        results = []

        if expiration:
            glob = _parquet_glob(date, expiration)
            count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
            results.append({"expiration": expiration, "rows": count})
        else:
            subfolders = sorted(e.name for e in folder.iterdir() if e.is_dir())
            if subfolders:
                for exp in subfolders:
                    try:
                        glob = _parquet_glob(date, exp)
                        count = con.execute(
                            f"SELECT COUNT(*) FROM read_parquet('{glob}')"
                        ).fetchone()[0]
                        results.append({"expiration": exp, "rows": count})
                    except Exception:
                        results.append({"expiration": exp, "rows": None})
            else:
                glob = f"{PARQUET_BASE}/{date}/*.parquet"
                count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
                results.append({"expiration": "(all)", "rows": count})

        con.close()
        total = sum(r["rows"] for r in results if r["rows"] is not None)
        return {"date": date, "expiration": expiration, "rows": results, "total": total}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DuckDB: {exc}")


@app.get("/api/parquet/preview")
def api_parquet_preview(date: str, expiration: str = None, limit: int = 50):
    _require_date(date)
    _date_path(date)  # validate path

    if expiration and not _SAFE_NAME_RE.match(expiration):
        raise HTTPException(status_code=400, detail="Invalid expiration value")

    limit = min(max(1, limit), 200)
    glob = _parquet_glob(date, expiration)

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


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, reload=False)
