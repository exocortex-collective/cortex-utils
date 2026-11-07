# cortex-utils

Operational utilities for Cortex: queue management, alerting, log tools.

## Features

- **Queue Management**: Partitioned queue with automatic maintenance
- **Dead Letter**: Failed job tracking and retry
- **Discord Alerter**: Log tailing with error classification and alerts

## Usage

```bash
# Queue stats
cortex-utils queue stats

# Partition maintenance
cortex-utils partitions maintain --retention-days 7

# Run alerter daemon
DISCORD_WEBHOOK_URL=... cortex-utils alerter run

# Send test alert
DISCORD_WEBHOOK_URL=... cortex-utils alerter test
```

## Environment Variables

- `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` - Database
- `DISCORD_WEBHOOK_URL` - Discord webhook for alerts
- `ALERTER_METRICS_PORT` - opt-in: serve the alerter's heartbeat (`cortex_alerter_heartbeat_timestamp_seconds`, `_containers_watched`, `_tailers_streaming`) on this port, for Prometheus to watch the alerter (cortex-ogqq). Unset: no port is opened.
- `ALERTER_METRICS_ADDR` - bind address for that port; default `127.0.0.1`. A container scraped from another container sets `0.0.0.0` explicitly.
