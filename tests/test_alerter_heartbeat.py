"""The alerter's heartbeat (cortex-ogqq): opt-in, loopback by default.

The alerter cannot report its own death, so Prometheus watches this heartbeat
instead. Other estates run the same code and must see no change unless they
opt in, so: no port without ALERTER_METRICS_PORT, and 127.0.0.1 unless
ALERTER_METRICS_ADDR says otherwise.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from cortex_utils.alerter import daemon as d


def _sample(name: str) -> float | None:
    return d.METRICS_REGISTRY.get_sample_value(name)


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
    def _daemon(self) -> d.AlerterDaemon:
        alerter = d.AlerterDaemon.__new__(d.AlerterDaemon)
        alerter._tailers = []
        return alerter

    def test_no_env_opens_no_port(self) -> None:
        with mock.patch.object(d, "start_http_server") as serve:
            self._daemon()._serve_metrics({})
        serve.assert_not_called()

    def test_opted_in_serves_its_own_registry_on_loopback(self) -> None:
        with mock.patch.object(d, "start_http_server") as serve:
            self._daemon()._serve_metrics({d.METRICS_PORT_ENV: "9105"})
        serve.assert_called_once_with(9105, addr="127.0.0.1", registry=d.METRICS_REGISTRY)


class TestBeat:
    def test_a_beat_stamps_the_time_and_counts_live_tailers(self) -> None:
        alerter = d.AlerterDaemon.__new__(d.AlerterDaemon)
        stop = threading.Event()
        live = threading.Thread(target=stop.wait, daemon=True)
        live.start()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        alerter._tailers = [live, dead]
        before = time.time()
        try:
            alerter._beat()
        finally:
            stop.set()
        assert _sample("cortex_alerter_heartbeat_timestamp_seconds") >= before
        assert _sample("cortex_alerter_tailers_alive") == 1

    def test_the_heartbeat_is_not_in_the_default_registry(self) -> None:
        """Importing the alerter must not add series to a host process's /metrics."""
        from prometheus_client import REGISTRY

        assert REGISTRY.get_sample_value("cortex_alerter_heartbeat_timestamp_seconds") is None
