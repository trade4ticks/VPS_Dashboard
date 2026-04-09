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
    "fetch_intraday": {
        "name": "SPX Intraday Fetch (cron)",
        "path": "/Thetadata_Raw_SPX/logs/fetch_intraday.log",
        "schedule": "1-59/5 * * * 1-5",
        "description": "Fetches SPX intraday option chain data every 5 minutes with a 1-minute delay to ensure data availability.",
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
