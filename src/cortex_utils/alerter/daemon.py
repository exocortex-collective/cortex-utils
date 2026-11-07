"""Alerter daemon that tails Docker logs and sends alerts to Discord."""

import os
import threading
import time
from datetime import datetime

import docker
import schedule
from prometheus_client import CollectorRegistry, Gauge, start_http_server

from cortex_utils.log import get_logger

from .classifier import Severity, _dedup_source, classify, is_error_line
from .discord import (
    COLOR_CRITICAL,
    COLOR_HIGH,
    COLOR_INFO,
    COLOR_WARNING,
    DiscordClient,
)
from .rate_limiter import (
    DEFAULT_ESCALATE_AFTER_MINUTES,
    DEFAULT_ESCALATE_AFTER_OCCURRENCES,
    RateLimiter,
    is_sustained,
)

log = get_logger()

# Everything under the prefix, minus a denylist. This was an allowlist of six
# names, which left 5 of 11 running containers unwatched -- including the
# gateway and the alerter itself. A denylist fails toward noise, which someone
# notices; an allowlist fails toward silence, which nobody does.
CONTAINER_PREFIX = "cortex-"

# The alerter cannot watch itself: if it dies it reads nothing, including its
# own logs. That needs an external watcher (~/HomeLab scripts/checks.d/), so it
# is excluded rather than implying coverage it cannot provide. cortex-ogqq.
DENYLISTED_CONTAINERS = frozenset({"cortex-alerter"})

# cortex-ogqq: the alerter cannot report its own death, so it publishes a
# heartbeat for Prometheus to watch (alerts then reach Discord through
# alertmanager, not through this process). OPT-IN: with ALERTER_METRICS_PORT
# unset no port is opened and nothing changes. The bind address defaults to
# loopback; a container that Prometheus scrapes from elsewhere sets
# ALERTER_METRICS_ADDR=0.0.0.0 explicitly.
METRICS_PORT_ENV = "ALERTER_METRICS_PORT"
METRICS_ADDR_ENV = "ALERTER_METRICS_ADDR"
DEFAULT_METRICS_ADDR = "127.0.0.1"
# its own registry, so importing this module adds nothing to a host
# process's default /metrics
METRICS_REGISTRY = CollectorRegistry()
HEARTBEAT = Gauge(
    "cortex_alerter_heartbeat_timestamp_seconds",
    "Unix time of the alerter scheduler loop's last pass (every ~60s)",
    registry=METRICS_REGISTRY,
)
CONTAINERS_WATCHED = Gauge(
    "cortex_alerter_containers_watched", "Containers the alerter tails", registry=METRICS_REGISTRY
)
TAILERS_ALIVE = Gauge(
    "cortex_alerter_tailers_alive", "Log-tailer threads still running", registry=METRICS_REGISTRY
)


def metrics_bind(environ: dict[str, str]) -> tuple[str, int] | None:
    """(addr, port) to serve the heartbeat on, or None when not enabled."""
    port = environ.get(METRICS_PORT_ENV, "").strip()
    if not port:
        return None
    return environ.get(METRICS_ADDR_ENV, "").strip() or DEFAULT_METRICS_ADDR, int(port)


def discover_containers(names: list[str]) -> list[str]:
    """Every cortex container except the denylisted ones.

    Takes the live container list rather than a hardcoded one, so a service
    added tonight is watched tonight.
    """
    return sorted(
        n for n in names if n.startswith(CONTAINER_PREFIX) and n not in DENYLISTED_CONTAINERS
    )


class AlerterDaemon:
    """Main alerter daemon that monitors Docker containers."""

    def __init__(
        self,
        webhook_url: str,
        containers: list[str] | None = None,
        ping_critical: bool = True,
        summary_hour: int = 6,
        escalate_after_minutes: int = DEFAULT_ESCALATE_AFTER_MINUTES,
        escalate_after_occurrences: int = DEFAULT_ESCALATE_AFTER_OCCURRENCES,
    ):
        """Initialize the alerter daemon.

        Args:
            webhook_url: Discord webhook URL
            containers: List of container names to monitor (default: cortex-* containers)
            ping_critical: Whether to @here on critical alerts
            summary_hour: Hour (0-23) to send daily summary
            escalate_after_minutes: A HIGH error still firing after this long is
                sent as CRITICAL, which pings. 0 disables escalation.
            escalate_after_occurrences: and only after this many occurrences, so
                a one-second burst of three lines is not read as an outage.
        """
        self.discord = DiscordClient(webhook_url)
        self.rate_limiter = RateLimiter()
        # None means DISCOVER once Docker is connected; run() fills it in.
        self.containers: list[str] = containers or []
        self._discover = not containers
        self.ping_critical = ping_critical
        self.summary_hour = summary_hour
        self.escalate_after_minutes = escalate_after_minutes
        self.escalate_after_occurrences = escalate_after_occurrences

        # Thread-safe lock for rate limiter access
        self._lock = threading.Lock()

        # Docker client - connect via socket
        self.docker_client: docker.DockerClient | None = None

        # Track running threads
        self._threads: list[threading.Thread] = []
        self._tailers: list[threading.Thread] = []
        self._stop_event = threading.Event()

    def _connect_docker(self) -> bool:
        """Connect to Docker daemon."""
        try:
            self.docker_client = docker.from_env()
            self.docker_client.ping()
            log.info("Connected to Docker daemon")
            return True
        except docker.errors.DockerException as e:
            log.error("Failed to connect to Docker", error=str(e))
            return False

    def _discover_running_containers(self) -> list[str]:
        """Live cortex containers, minus the denylist.

        Falls back to an empty list rather than a stale hardcoded set: an
        alerter that cannot see Docker should say so loudly at startup, not
        quietly watch six names that may no longer be the right six.
        """
        if self.docker_client is None:
            log.error("Container discovery before Docker connect; watching NOTHING")
            return []
        try:
            names = [c.name for c in self.docker_client.containers.list()]
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            log.error("Cannot list containers; alerter is watching NOTHING", error=str(exc))
            return []
        found = discover_containers(names)
        log.info("Discovered containers to watch", count=len(found), containers=found)
        return found

    def _process_log_line(self, container: str, log_line: str) -> None:
        """Process a single log line and send alerts if needed."""

        def send(title: str, description: str, color: int, log_limit: int, ping: bool) -> None:
            """The one shape every alert embed has."""
            self.discord.send_embed(
                title=title,
                description=description,
                color=color,
                fields=[
                    {"name": "Container", "value": container, "inline": True},
                    {
                        "name": "Time",
                        "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "inline": True,
                    },
                    {"name": "Log", "value": f"```{log_line[:log_limit]}```", "inline": False},
                ],
                ping=ping,
            )

        # Quick filter - only classify lines that look like errors
        if not is_error_line(log_line):
            return

        # Classify the error
        classification = classify(container, log_line)
        if classification is None:
            return

        with self._lock:
            if classification.severity == Severity.CRITICAL:
                # Critical: always check rate limiter (some have 0 cooldown = always alert)
                if self.rate_limiter.should_alert(
                    classification.error_key, classification.cooldown_minutes
                ):
                    log.warning(
                        "Critical error detected",
                        container=container,
                        title=classification.title,
                    )
                    send(
                        f"CRITICAL: {classification.title}",
                        classification.description,
                        COLOR_CRITICAL,
                        500,
                        self.ping_critical,
                    )

            elif classification.severity == Severity.HIGH:
                # Record EVERY occurrence, including the ones the cooldown is
                # about to swallow: those suppressed lines are the only
                # evidence that an outage is sustained rather than a blip.
                run = self.rate_limiter.note_occurrence(classification.error_key)
                now = datetime.now()
                if self.escalate_after_minutes > 0 and is_sustained(
                    run, now, self.escalate_after_minutes, self.escalate_after_occurrences
                ):
                    minutes = int((now - run.started).total_seconds() // 60)
                    log.warning(
                        "Sustained error escalated to critical",
                        container=container,
                        title=classification.title,
                        error_key=classification.error_key,
                        occurrences=run.count,
                        minutes=minutes,
                    )
                    send(
                        f"CRITICAL: {classification.title} (sustained)",
                        f"{classification.description}\n\n"
                        f"Still failing after {minutes} minutes and "
                        f"{run.count} occurrences; this is not transient.",
                        COLOR_CRITICAL,
                        500,
                        self.ping_critical,
                    )
                    # Once per outage; see mark_escalated.
                    self.rate_limiter.mark_escalated(classification.error_key)
                elif self.rate_limiter.should_alert(
                    classification.error_key, classification.cooldown_minutes
                ):
                    log.info(
                        "High priority error detected",
                        container=container,
                        title=classification.title,
                    )
                    send(
                        f"Warning: {classification.title}",
                        classification.description,
                        COLOR_HIGH,
                        300,
                        False,
                    )

            elif classification.severity == Severity.WARNING:
                # Counted for the daily summary. The sample is the
                # NORMALISED message, so it describes the whole bucket rather
                # than whichever occurrence arrived first.
                self.rate_limiter.increment_warning(
                    classification.error_key, _dedup_source(log_line)
                )
                log.debug(
                    "Warning counted",
                    container=container,
                    title=classification.title,
                )

    def _tail_container(self, container_name: str) -> None:
        """Tail logs from a specific container."""
        while not self._stop_event.is_set():
            try:
                if self.docker_client is None:
                    log.error("Docker client not connected")
                    time.sleep(30)
                    continue

                container = self.docker_client.containers.get(container_name)
                log.info("Starting log tail", container=container_name)

                # Tail logs from now onwards
                for log_entry in container.logs(
                    stream=True, follow=True, since=datetime.now(), timestamps=False
                ):
                    if self._stop_event.is_set():
                        break

                    line = log_entry.decode("utf-8", errors="replace").strip()
                    if line:
                        self._process_log_line(container_name, line)

            except docker.errors.NotFound:
                log.warning("Container not found, will retry", container=container_name)
                time.sleep(60)  # Wait before retrying

            except docker.errors.APIError as e:
                log.error("Docker API error", container=container_name, error=str(e))
                time.sleep(30)

            except Exception:
                log.exception("Unexpected error tailing container", container=container_name)
                time.sleep(30)

    def _send_daily_summary(self) -> None:
        """Send daily summary of warnings."""
        # READ, do not reset -- see _clear_if_delivered. Resetting here and
        # ignoring the send result meant one Discord 5xx deleted the day.
        with self._lock:
            samples = dict(self.rate_limiter.warning_samples)
            counts = self.rate_limiter.get_warning_counts()

        if not counts:
            # No warnings to report
            log.info("Daily summary: no warnings")
            date_str = datetime.now().strftime("%Y-%m-%d")
            self._clear_if_delivered(
                self.discord.send_embed(
                    title="Cortex Daily Summary",
                    description=f"**{date_str}**\n\nNo warnings or errors to report.",
                    color=COLOR_INFO,
                    ping=False,
                )
            )
            return

        # Format warning counts
        warning_lines = []
        for error_key, count in sorted(counts.items(), key=lambda x: -x[1]):
            # error_key format: "container:error_type"
            parts = error_key.split(":", 1)
            container = parts[0] if len(parts) > 1 else "unknown"
            error_type = parts[1].replace("_", " ") if len(parts) > 1 else error_key
            # .title() used to be applied here and case-mangled the hex digest,
            # so the string printed was not the key anyone could grep for.
            if not error_type.startswith("unclassified"):
                error_type = error_type.title()
            line = f"- **{error_type}** ({container}): {count}"
            sample = samples.get(error_key, "").strip()
            if sample:
                line += f"\n  {sample[:120]}"
            warning_lines.append(line)

        description = f"**{datetime.now().strftime('%Y-%m-%d')}**\n\n"
        description += "**Warnings:**\n" + "\n".join(warning_lines[:20])  # Limit to 20 items

        if len(counts) > 20:
            description += f"\n... and {len(counts) - 20} more"

        log.info("Sending daily summary", warning_count=len(counts))
        self._clear_if_delivered(
            self.discord.send_embed(
                title="Cortex Daily Summary",
                description=description,
                color=COLOR_WARNING if counts else COLOR_INFO,
                ping=False,
            ),
            len(counts),
        )

    def _clear_if_delivered(self, delivered: bool, warning_count: int = 0) -> None:
        """Drop the day's warnings only once Discord has accepted them.

        A failed send keeps them for the next summary. A warning arriving
        between the read and this reset is lost, but that window is one HTTP
        request wide.
        """
        if not delivered:
            log.error(
                "Daily summary was NOT delivered; keeping the day's warnings",
                warning_count=warning_count,
            )
            return
        with self._lock:
            self.rate_limiter.reset_warning_counts()

    def _schedule_loop(self) -> None:
        """Run the scheduler loop.

        schedule.run_pending() does not catch, and this runs on a DAEMON
        thread: one raise out of a job killed the thread permanently, every
        later summary was silently never sent, and the process went on looking
        healthy while warning_counts filled with nothing draining it. Catch,
        report, keep the loop alive.
        """
        while not self._stop_event.is_set():
            try:
                schedule.run_pending()
            except Exception:
                log.exception("Scheduled job raised; the scheduler stays up")
            self._beat()
            time.sleep(60)

    def _serve_metrics(self, environ: dict[str, str]) -> None:
        bind = metrics_bind(environ)
        if bind is None:
            return
        start_http_server(bind[1], addr=bind[0], registry=METRICS_REGISTRY)
        log.info("Heartbeat metrics serving", addr=bind[0], port=bind[1])

    def _beat(self) -> None:
        HEARTBEAT.set(time.time())
        TAILERS_ALIVE.set(sum(1 for t in self._tailers if t.is_alive()))

    def send_test_alert(self) -> bool:
        """Send a test alert to verify webhook is working.

        `self.containers` is EMPTY until run() fills it from discovery, and
        `cortex alerter test` calls this before run() -- so replacing the old
        `containers or DEFAULT_CONTAINERS` with `containers or []` made this
        render an empty Containers field where it used to name six. Discord's
        schema requires a non-empty field value, and an operator running the
        test learns nothing from a blank list.

        Discovery is best-effort here: a webhook test must still work when
        Docker does not, so a failure falls back to an honest string rather
        than to silence or an exception.
        """
        watched = self.containers
        if not watched and self._discover and self._connect_docker():
            watched = self._discover_running_containers()
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.discord.send_embed(
            title="Test Alert",
            description="Alerter is configured correctly.",
            color=COLOR_INFO,
            fields=[
                {"name": "Time", "value": time_str, "inline": True},
                {
                    "name": "Containers",
                    "value": ", ".join(watched) or "(none discovered -- Docker unreachable?)",
                    "inline": False,
                },
            ],
            ping=False,
        )

    def run(self) -> None:
        """Start the alerter daemon."""
        log.info("Starting alerter daemon")

        # Connect to Docker
        if not self._connect_docker():
            log.error("Cannot start without Docker connection")
            return

        # Discovery needs the connected client, so it happens HERE rather than
        # in __init__. An explicit -c list still wins; this is the default path.
        if self._discover:
            self.containers = self._discover_running_containers()
        if not self.containers:
            log.error("Alerter is watching NOTHING -- no containers to monitor")
        log.info("Monitoring containers", count=len(self.containers), names=self.containers)

        # Schedule daily summary
        schedule.every().day.at(f"{self.summary_hour:02d}:00").do(self._send_daily_summary)
        log.info("Daily summary scheduled", hour=self.summary_hour)

        # Start scheduler thread
        scheduler_thread = threading.Thread(target=self._schedule_loop, daemon=True)
        scheduler_thread.start()
        self._threads.append(scheduler_thread)

        # Start log tailer threads for each container
        for container_name in self.containers:
            thread = threading.Thread(
                target=self._tail_container,
                args=(container_name,),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
            self._tailers.append(thread)
            log.info("Started log tailer", container=container_name)

        CONTAINERS_WATCHED.set(len(self.containers))
        self._serve_metrics(dict(os.environ))
        self._beat()

        # Send startup notification
        self.discord.send(
            f"Alerter started. Monitoring {len(self.containers)} containers.",
        )

        # Keep main thread alive
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            log.info("Received shutdown signal")
            self.stop()

    def stop(self) -> None:
        """Stop the alerter daemon."""
        log.info("Stopping alerter daemon")
        self._stop_event.set()

        # Send shutdown notification
        self.discord.send("Alerter shutting down.")


def run_alerter(
    webhook_url: str | None = None,
    containers: list[str] | None = None,
    ping_critical: bool = True,
    summary_hour: int = 6,
    escalate_after_minutes: int = DEFAULT_ESCALATE_AFTER_MINUTES,
    escalate_after_occurrences: int = DEFAULT_ESCALATE_AFTER_OCCURRENCES,
) -> None:
    """Run the alerter daemon.

    Args:
        webhook_url: Discord webhook URL (or from DISCORD_WEBHOOK_URL env)
        containers: Containers to monitor (default: cortex-* containers)
        ping_critical: Whether to @here on critical alerts
        summary_hour: Hour to send daily summary
        escalate_after_minutes: Send a still-firing HIGH error as CRITICAL after
            this long (0 disables)
        escalate_after_occurrences: and only after this many occurrences
    """
    url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise ValueError(
            "Discord webhook URL required (set DISCORD_WEBHOOK_URL or pass webhook_url)"
        )

    daemon = AlerterDaemon(
        webhook_url=url,
        containers=containers,
        ping_critical=ping_critical,
        summary_hour=summary_hour,
        escalate_after_minutes=escalate_after_minutes,
        escalate_after_occurrences=escalate_after_occurrences,
    )
    daemon.run()
