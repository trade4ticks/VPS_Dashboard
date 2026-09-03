import os

PORT = 8080

# Systemd services to monitor and control.
# service: the systemd unit name (None = no restart button)
# port: checked for reachability (None = skip port check)
SERVICES = {
    "thetadata": {
        "name": "ThetaData Terminal",
        "service": "thetadata.service",
        "host": "localhost",
        "port": 25503,
    },
    "portfolio_dashboard": {
        "name": "Portfolio Dashboard",
        "service": "portfolio-dashboard",
        "host": "localhost",
        "port": 8050,
    },
    "postgres": {
        "name": "PostgreSQL",
        "service": None,       # runs in Docker, no systemd unit
        "host": "localhost",
        "port": 5432,
    },
    "spx_dashboard": {
        "name": "SPX Analysis Dashboard",
        "service": "spx-dashboard.service",
        "host": "localhost",
        "port": 8000,
    },
    "spx_live": {
        "name": "SPX Live Tape",
        "service": "spx-live.service",
        "host": "localhost",
        "port": 8001,
    },
    "vps_dashboard": {
        "name": "VPS Dashboard",
        "service": "vps_dashboard.service",
        "host": "localhost",
        "port": 8080,
    },
    "cloudflared": {
        "name": "Cloudflare Tunnel",
        "service": "cloudflared.service",
        "host": "localhost",
        "port": None,          # outbound tunnel, no local port to check
    },
}

# Git projects — shown on the Services page with pull/deploy buttons.
# service: systemd unit to restart after git pull (None = pull only)
PROJECTS = {
    "portfolio_dashboard": {
        "name": "Portfolio Dashboard",
        "path": "/root/Portfolio_Dashboard",
        "service": "portfolio-dashboard",
    },
    "thetadata_raw_spx": {
        "name": "Thetadata Raw SPX",
        "path": "/root/Thetadata_Raw_SPX",
        "service": None,
    },
    "spx_analysis_dashboard": {
        "name": "SPX Analysis Dashboard",
        "path": "/spx_analysis_dashboard",
        # One repo, two units: a single pull updates both, and each needs its
        # own restart. Deploy restarts them in the order listed. Use "services"
        # (list) for multi-unit projects; "service" (str) still works for one.
        "services": ["spx-dashboard.service", "spx-live.service"],
    },
    "vps_dashboard": {
        "name": "VPS Dashboard",
        "path": "/root/VPS_Dashboard",
        "service": "vps_dashboard.service",
    },
    "clean_spx": {
        "name": "Clean SPX",
        "path": "/clean_SPX",
        "service": None,
    },
    "interpolate_spx": {
        "name": "Interpolate SPX",
        "path": "/interpolate_SPX",
        "service": None,
    },
    "spx_surface_snapshot": {
        "name": "SPX Surface Snapshot",
        "path": "/spx_surface_snapshot",
        "service": None,
    },
}

# Live-trading arm/disarm control, served by the spx-live unit. The endpoint
# is localhost-only and unauthenticated by design: the safety property is that
# arming can only be done from the VPS itself, so the public trading page
# cannot place orders on its own. This dashboard proxies it -- see /api/trading.
TRADING_CONTROL = {
    "name": "SPX Live Trading",
    "base_url": "http://127.0.0.1:8001",
    "path": "/trading",
    "timeout": 5,
}

# Log files shown on the Logs page.
LOG_FILES = {
    "fetch_trades": {
        "name": "Fetch Trades (cron)",
        "path": "/root/Portfolio_Dashboard/logs/fetch_trades.log",
        "schedule": "*/5 * * * 1-5",
        "description": "Fetches filled transactions from brokers. Weekdays, every 5 min.",
        "runs": [{
            "label": "Run now",
            "lock": "/tmp/fetch_trades.lock",
            "command": "flock -n /tmp/fetch_trades.lock /root/Portfolio_Dashboard/.venv/bin/python /root/Portfolio_Dashboard/scripts/fetch_trades.py >> /root/Portfolio_Dashboard/logs/fetch_trades.log 2>&1",
        }],
        # Cron status detection (shown on Overview). Tracks Schwab only;
        # the Tasty 403s are expected (access disabled) and ignored.
        "status": {
            "lines": 60,
            "label": "Schwab",
            "ts_regex": r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            "ts_format": "%Y-%m-%d %H:%M:%S",
            "success_regex": r"\[Schwab\] DB write:",
            "failure_regex": r"ERROR\s+\[Schwab\]",
        },
    },
    "pg_backup": {
        "name": "Portfolio Trades Backup (cron)",
        "path": "/root/Portfolio_Dashboard/logs/pg_backup.log",
        "schedule": "0 2 * * *",
        "description": "Backup of Portfolio Dashboard to Google Drive. Nightly 2am.",
        # "2026-08-25 02:00:07 INFO Backup successful - portfolio_....sql (2.0M)"
        "status": {
            "lines": 40,
            "label": "Backup",
            "ts_regex": r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            "ts_format": "%Y-%m-%d %H:%M:%S",
            "success_regex": r"Backup successful",
            "failure_regex": r"Backup failed|Traceback|CRITICAL|ERROR",
        },
    },
    "spx_pipeline": {
        "name": "SPX Pipeline (cron)",
        "path": "/Thetadata_Raw_SPX/logs/pipeline.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Runs the SPX intraday pipeline every 5 minutes with a 1-minute delay, orchestrating fetch, clean, interpolate, surface snapshot, and index OHLC steps.",
        "runs": [{
            "label": "Run now",
            "lock": "/tmp/spx_pipeline.lock",
            "command": "flock -n /tmp/spx_pipeline.lock /Thetadata_Raw_SPX/.venv/bin/python /Thetadata_Raw_SPX/run_pipeline.py >> /Thetadata_Raw_SPX/logs/pipeline.log 2>&1",
        }],
        # Each cycle ends with "Pipeline complete"; most recent marker wins.
        "status": {
            "lines": 80,
            "label": "Run",
            "ts_regex": r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]",
            "ts_format": "%Y-%m-%d %H:%M:%S",
            "success_regex": r"Pipeline complete",
            "failure_regex": r"Traceback|ERROR|aborting|FAILED",
        },
    },
    "fetch_intraday": {
        "name": "SPX Intraday Fetch (cron)",
        "path": "/Thetadata_Raw_SPX/logs/fetch_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Fetches SPX intraday option chain data every 5 minutes with a 1-minute delay to ensure data availability.",
    },
    "clean_intraday": {
        "name": "SPX Intraday Clean (cron)",
        "path": "/clean_SPX/logs/process_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Triggered by the SPX pipeline to clean and enrich newly fetched intraday SPX option chain data.",
    },
    "interpolate_intraday": {
        "name": "SPX Intraday Interpolate (cron)",
        "path": "/interpolate_SPX/logs/process_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Triggered by the SPX pipeline to interpolate and smooth cleaned intraday SPX option chain data into the surface database.",
    },
    "surface_snapshot_intraday": {
        "name": "SPX Surface Snapshot (cron)",
        "path": "/spx_surface_snapshot/logs/process_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Processes first pass surface snapshot data.",
    },
    "surface_snapshot_followup_intraday": {
        "name": "SPX Surface Snapshot Followup (cron)",
        "path": "/spx_surface_snapshot/logs/process_intraday_followup.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Processes followup pass surface snapshot data.",
    },
    "update_index_ohlc": {
        "name": "Index OHLC (cron)",
        "path": "/Thetadata_Raw_SPX/Logs/update_index_ohlc.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Fetches SPX, VIX, VIX3M & VIX9D intraday 5min OHLC from yfinance.",
    },
    "oi_pipeline": {
        "name": "OI Pipeline (cron)",
        "path": "/Open_Interest/logs/pipeline.log",
        "schedule": "0 7 * * *",
        "description": "Runs the Open Interest data pipeline.",
        "runs": [
            {"label": "EVENING",   "lock": "/tmp/oi_research.lock", "command": "flock -n /tmp/oi_research.lock /Open_Interest/.venv/bin/python /Open_Interest/run_pipeline.py --tier EVENING >> /Open_Interest/logs/pipeline.log 2>&1"},
            {"label": "PREMARKET", "lock": "/tmp/oi_research.lock", "command": "flock -n /tmp/oi_research.lock /Open_Interest/.venv/bin/python /Open_Interest/run_pipeline_early.py >> /Open_Interest/logs/pipeline.log 2>&1"},
            {"label": "MORNING",   "lock": "/tmp/oi_research.lock", "command": "flock -n /tmp/oi_research.lock /Open_Interest/.venv/bin/python /Open_Interest/run_pipeline.py --tier MORNING >> /Open_Interest/logs/pipeline.log 2>&1"},
        ],
        # Per-tier status. Each run logs its tier + date in the start line
        # (e.g. "Pipeline starting (today = 2026-06-16, tier = MORNING)").
        # Log times are ET (cron sets TZ=America/New_York).
        # An OI run logs per-ticker (~900 lines), so tailing can't reach back
        # far enough to cover all tiers. Instead we grep only the sparse run
        # markers (headers + outcomes) and pair them up in file order.
        "status": {
            "tiered": True,
            "marker_lines": 400,  # last N grepped marker lines (covers many runs)
            "time_note": "ET",
            "tiers": ["PREMARKET", "MORNING", "EVENING"],
            # Marker lines to extract: run headers, completions, aborts.
            "grep_regex": r"[Pp]ipeline starting|[Pp]ipeline complete|aborting",
            # A run header: "Pipeline starting (today = D, tier = X)" or the
            # premarket "Early pipeline starting (today = D)".
            "start_regex": r"[Pp]ipeline starting",
            "premarket_regex": r"Early pipeline starting",
            "tier_regex": r"tier = (\w+)",
            "date_in_start_regex": r"today = (\d{4}-\d{2}-\d{2})",
            "time_regex": r"^(\d{2}:\d{2}:\d{2})",
            # Completion: "Pipeline complete ..." or "Early pipeline complete ...".
            "success_regex": r"[Pp]ipeline complete",
            "failure_regex": r"aborting|Traceback|FAILED",
            "warn_regex": r"bin_build_rc = [1-9]",
        },
    },
    "equity_iv_intraday": {
        "name": "Equity IV Intraday (cron)",
        "path": "/Open_Interest/logs/live_pipeline.log",
        "schedule": "*/5 9-16 * * 1-5",
        "description": "Fetches live equity chain snapshots, cleans the data, builds the interpolated surface, and calculates metrics.",
        # A run ends with a "=== pipeline ===" block whose last line is
        # "  total    84s". Those summary lines carry no timestamp, so the run
        # time is carried forward from the run's "Log: ..._YYYYMMDD_HHMMSS.log".
        "status": {
            "lines": 120,
            "label": "Equity IV Intraday",
            "ts_regex": r"fetch_live_surface_(\d{8}_\d{6})\.log",
            "ts_format": "%Y%m%d_%H%M%S",
            "ts_carry": True,
            "display_format": "%Y-%m-%d %H:%M",
            "success_regex": r"^\s*total\s+\d+s",
            "failure_regex": r"Traceback|CRITICAL|aborting",
        },
    },
    "earnings": {
        "name": "Earnings (cron)",
        "path": "/Open_Interest/logs/earnings.log",
        "schedule": "30 20 * * 1-5",
        "description": "Nightly update of future earnings dates.",
        # A run ends with "  calendar rows   2262 upserted". As with the live
        # surface log the summary lines are untimestamped, so the run time is
        # carried from "Log: .../fetch_earnings_calendar_YYYYMMDD_HHMMSS.log".
        "status": {
            "lines": 120,
            "label": "Earnings",
            "ts_regex": r"fetch_earnings_calendar_(\d{8}_\d{6})\.log",
            "ts_format": "%Y%m%d_%H%M%S",
            "ts_carry": True,
            "display_format": "%Y-%m-%d %H:%M",
            "success_regex": r"calendar rows\s+[\d,]+\s+upserted",
            # NOTE: a healthy run still prints "FAILED  1  - SMCI" -- that is a
            # per-ticker tally, NOT a failed run. Matching it as a failure would
            # mark every good run red, so it drives the warning badge instead
            # and is deliberately absent from failure_regex.
            "failure_regex": r"Traceback|CRITICAL|aborting",
            "warn_regex": r"^\s*FAILED\s+[1-9]",
        },
    },
    "ai_explorer": {
        "name": "AI Explorer Log",
        "type": "postgres",
        "table": "ai_explorer_log",
        "description": "Query and response log from the AI Explorer page.",
    },
}

# Overview page "Cron Jobs -- Last Run" grid. Each entry renders one card and
# concatenates the run rows of its member LOG_FILES keys, so several jobs can
# share a card. Members with a "status" block get real success/failure
# detection; members without one fall back to a neutral row showing the log's
# last-write time (mtime), which is when the job last produced output.
# "name" is optional -- a single-member card falls back to that job's name.
CRON_OVERVIEW_CARDS = [
    {"members": ["fetch_trades"]},
    {"members": ["spx_pipeline"]},
    {
        "name": "Equity Analysis",
        "members": ["oi_pipeline", "equity_iv_intraday", "earnings"],
    },
    {"members": ["pg_backup"]},
]

# Non-project directories shown in the File Browser (no git/deploy buttons).
BROWSE_PATHS = {
    "spx_options": {
        "name": "SPX Options Data",
        "path": "/data/spx_options",
    },
    "spx_options_vol1": {
        "name": "SPX Options Data (Volume 1)",
        "path": "/mnt/volume1/spx_options",
    },
    "spx_options_vol2": {
        "name": "SPX Options Data (Volume 2)",
        "path": "/mnt/volume2/spx_options",
    },
    "volume3": {
        "name": "Volume 3",
        "path": "/mnt/trading_volume_3",
    },
}

# Directories tracked on the Disk page (per-path subdirectory breakdown).
DISK_PATHS = {
    "SPX Options (Parquet)": "/data/spx_options",
    "OI Raw (Parquet)": "/data/oi_raw",
    # The block-storage volumes are deliberately NOT listed here. Every entry in
    # this dict is walked recursively (two rglob passes + a stat per file) on
    # each /api/disk call, and these volumes hold years of date-partitioned
    # parquet leaves. Their usage comes from the DISK_VOLUMES df cards instead,
    # which is filesystem-level and costs the same at any file count:
    #   /mnt/volume1           -> "Block Storage 1"
    #   /mnt/volume2           -> "Block Storage 2"
    #   /mnt/trading_volume_3  -> "Block Storage 3"
    # If per-directory breakdowns are wanted again, precompute them in a nightly
    # job that writes a summary file for this endpoint to read -- not by walking
    # the tree on request.
}

# Filesystems shown on the Disk page (df usage cards).
DISK_VOLUMES = {
    "Root Filesystem": "/",
    "Block Storage 1": "/mnt/volume1",
    "Block Storage 2": "/mnt/volume2",
    "Block Storage 3": "/mnt/trading_volume_3",
}

# PostgreSQL connection for Disk page DB/table size stats.
# Set POSTGRES_PASSWORD env var in the systemd unit (Environment=POSTGRES_PASSWORD=...).
POSTGRES_CONN = {
    "host": "localhost",
    "port": 5432,
    "user": "portfolio",
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
}

# Parquet data sources available in the Data Inspector.
PARQUET_SOURCES = {
    "root_volume": {
        "name": "Root Volume",
        "path": "/data/spx_options",
    },
    "block_storage_1": {
        "name": "Block Storage 1",
        "path": "/mnt/volume1/spx_options",
    },
    "block_storage_2": {
        "name": "Block Storage 2",
        "path": "/mnt/volume2/spx_options",
    },
    "block_storage_3": {
        "name": "Block Storage 3",
        "path": "/mnt/trading_volume_3",
    },
}
