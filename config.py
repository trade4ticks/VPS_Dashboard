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
}

# Log files shown on the Logs page.
LOG_FILES = {
    "fetch_trades": {
        "name": "Fetch Trades (Cron)",
        "path": "/root/Portfolio_Dashboard/logs/fetch_trades.log",
        "schedule": "*/5 * * * 1-5",
        "description": "Fetches filled transactions from brokers. Weekdays, every 5 min.",
    },
    "spx_pipeline": {
        "name": "SPX Pipeline (Cron)",
        "path": "/Thetadata_Raw_SPX/logs/pipeline.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Runs the SPX intraday pipeline every 5 minutes with a 1-minute delay, orchestrating fetch, clean, interpolate, surface snapshot, and index OHLC steps.",
    },
    "fetch_intraday": {
        "name": "SPX Intraday Fetch (cron)",
        "path": "/Thetadata_Raw_SPX/logs/fetch_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Fetches SPX intraday option chain data every 5 minutes with a 1-minute delay to ensure data availability.",
    },
    "clean_intraday": {
        "name": "SPX Intraday Clean",
        "path": "/clean_SPX/logs/process_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Triggered by the SPX pipeline to clean and enrich newly fetched intraday SPX option chain data.",
    },
    "interpolate_intraday": {
        "name": "SPX Intraday Interpolate",
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
    "update_index_ohlc": {
        "name": "Index OHLC (cron)",
        "path": "/Thetadata_Raw_SPX/Logs/update_index_ohlc.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Fetches SPX, VIX, VIX3M & VIX9D intraday 5min OHLC from yfinance.",
    },
}

# Non-project directories shown in the File Browser (no git/deploy buttons).
BROWSE_PATHS = {
    "spx_options": {
        "name": "SPX Options Data",
        "path": "/data/spx_options",
    },
}

# Directories tracked on the Disk page.
DISK_PATHS = {
    "SPX Options (Parquet)": "/data/spx_options",
}
