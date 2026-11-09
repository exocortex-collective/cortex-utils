"""The alerter's heartbeat (cortex-ogqq): opt-in, loopback by default.

The alerter cannot report its own death, so Prometheus watches this heartbeat
instead. Other estates run the same code and must see no change unless they
opt in, so: no port without ALERTER_METRICS_PORT, and 127.0.0.1 unless
ALERTER_METRICS_ADDR says otherwise.
"""

from __future__ import annotations

import os
import time
from unittest import mock

import docker

from cortex_utils.alerter import daemon as d


def _sample(name: str) -> float | None:
    return d.METRICS_REGISTRY.get_sample_value(name)


def _daemon(containers: list[str] | None = None) -> d.AlerterDaemon:
    with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
        return d.AlerterDaemon("https://discord.test/webhook", containers=containers)


class TestMetricsBind:
    def test_unset_means_no_port(self) -> None:
        assert d.metrics_bind({}) is None
        assert d.metrics_bind({d.METRICS_PORT_ENV: "  "}) is None

    def test_the_bind_address_defaults_to_loopback(self) -> None:
        """valid's requirement: only an explicit setting widens the bind."""
        assert d.DEFAULT_METRICS_ADDR == "127.0.0.1"
        assert d.metrics_bind({d.METRICS_PORT_ENV: "9105"}) == ("127.0.0.1", 9105)
        blank_addr = {d.METRICS_PORT_ENV: "9105", d.METRICS_ADDR_ENV: ""}
        assert d.metrics_bind(blank_addr) == ("127.0.0.1", 9105)

    def test_an_explicit_address_is_used(self) -> None:
        env = {d.METRICS_PORT_ENV: "9105", d.METRICS_ADDR_ENV: "0.0.0.0"}
        assert d.metrics_bind(env) == ("0.0.0.0", 9105)


class TestServeMetrics:
    def test_no_env_opens_no_port(self) -> None:
        with mock.patch.object(d, "start_http_server") as serve:
            _daemon()._serve_metrics({})
        serve.assert_not_called()

    def test_opted_in_serves_its_own_registry_on_loopback(self) -> None:
        with mock.patch.object(d, "start_http_server") as serve:
            _daemon()._serve_metrics({d.METRICS_PORT_ENV: "9105"})
        serve.assert_called_once_with(9105, addr="127.0.0.1", registry=d.METRICS_REGISTRY)

    def test_a_bad_port_or_one_in_use_is_logged_not_fatal(self) -> None:
        """The alerter keeps alerting; Prometheus sees the heartbeat target down."""
        alerter = _daemon()
        with mock.patch.object(d, "log") as logger:
            alerter._serve_metrics({d.METRICS_PORT_ENV: "not-a-port"})
            with mock.patch.object(d, "start_http_server", side_effect=OSError("in use")):
                alerter._serve_metrics({d.METRICS_PORT_ENV: "9105"})
        assert logger.exception.call_count == 2


class TestWiring:
    def _run_once(self, alerter: d.AlerterDaemon, env: dict[str, str]) -> mock.Mock:
        alerter._stop_event.set()  # run() returns at its final wait()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(alerter, "_connect_docker", return_value=True),
            mock.patch.object(alerter, "_tail_container"),
            mock.patch("cortex_utils.alerter.daemon.schedule"),
            mock.patch.object(d, "start_http_server") as serve,
        ):
            alerter.run()
        return serve

    def test_run_serves_beats_and_counts_when_opted_in(self) -> None:
        alerter = _daemon(containers=["cortex-a", "cortex-b", "cortex-c"])
        before = time.time()
        env = {d.METRICS_PORT_ENV: "9105", d.METRICS_ADDR_ENV: "0.0.0.0"}
        serve = self._run_once(alerter, env)
        serve.assert_called_once_with(9105, addr="0.0.0.0", registry=d.METRICS_REGISTRY)
        assert _sample("cortex_alerter_containers_watched") == 3
        heartbeat = _sample("cortex_alerter_heartbeat_timestamp_seconds")
        assert heartbeat is not None and heartbeat >= before

    def test_run_opens_nothing_without_the_env(self) -> None:
        self._run_once(_daemon(containers=["cortex-a"]), {}).assert_not_called()

    def test_the_scheduler_loop_beats_every_pass(self) -> None:
        alerter = _daemon(containers=["cortex-a"])
        with (
            mock.patch("cortex_utils.alerter.daemon.schedule"),
            mock.patch.object(d.time, "sleep", side_effect=lambda _s: alerter._stop_event.set()),
            mock.patch.object(alerter, "_beat") as beat,
        ):
            alerter._schedule_loop()
        beat.assert_called_once()


class TestStreaming:
    def test_a_tailer_counts_only_while_it_holds_a_stream(self) -> None:
        alerter = _daemon(containers=["cortex-a"])
        seen: list[float | None] = []

        def stream(**_kw: object) -> None:
            alerter._beat()
            seen.append(_sample("cortex_alerter_tailers_streaming"))
            raise docker.errors.APIError("socket gone")

        alerter.docker_client = mock.Mock()
        alerter.docker_client.containers.get.return_value.logs.side_effect = stream
        with mock.patch.object(d.time, "sleep", side_effect=lambda _s: alerter._stop_event.set()):
            alerter._tail_container("cortex-a")
        alerter._beat()
        assert seen == [1] and _sample("cortex_alerter_tailers_streaming") == 0

    def test_a_stopped_container_makes_the_two_gauges_diverge(self) -> None:
        """What utils-a6ba is for. `tailers_streaming < containers_watched`
        could not fire for a dead container: discovery read the RUNNING list,
        so both numbers fell together and 10 < 10 stayed clean. Keeping the
        stopped container in the watch set drops the numerator alone.
        """
        alerter = _daemon()
        alerter.docker_client = mock.Mock()

        def listing(all: bool = False) -> list[dict]:  # noqa: A002 -- docker-py's name
            names = ["cortex-live", "cortex-dead"] if all else ["cortex-live"]
            return [{"Names": [f"/{n}"]} for n in names]

        alerter.docker_client.api.containers.side_effect = listing
        alerter._stop_event.set()  # run() returns at its final wait()
        with (
            mock.patch.object(alerter, "_connect_docker", return_value=True),
            mock.patch.object(alerter, "_tail_container"),
            mock.patch("cortex_utils.alerter.daemon.schedule"),
        ):
            alerter.run()
        alerter._streaming["cortex-live"] = True  # the dead one never gets a stream
        alerter._beat()
        watched = _sample("cortex_alerter_containers_watched")
        streaming = _sample("cortex_alerter_tailers_streaming")
        assert (watched, streaming) == (2, 1), (watched, streaming)

    def test_the_heartbeat_is_not_in_the_default_registry(self) -> None:
        """Importing the alerter must not add series to a host process's /metrics."""
        from prometheus_client import REGISTRY

        assert REGISTRY.get_sample_value("cortex_alerter_heartbeat_timestamp_seconds") is None
