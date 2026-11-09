"""The alerter's defaults, all of which used to fail toward silence.

Three things were wrong and each hid the next: the gate could not see
structlog's "level" field, the dedup key hashed a per-emit timestamp, and the
daily summary printed a count with no message. Measured before the change, of
67 error messages cortex can emit, zero could reach a human.

Every test here asserts one of those stays fixed. They are written against
real Hades log lines rather than synthetic ones, because the defects were all
in the difference between the two.
"""

from __future__ import annotations

import json
from unittest import mock

import docker
import pytest

from cortex_utils.alerter.classifier import (
    Severity,
    classify,
    is_client_sql_fault,
    is_error_line,
)
from cortex_utils.alerter.daemon import DENYLISTED_CONTAINERS, discover_containers


class TestContainerDiscovery:
    def test_a_new_cortex_service_is_watched_without_anyone_editing_a_list(self) -> None:
        """The whole point: adding a service must not silently add a blind spot."""
        running = ["cortex-gmail-sync", "cortex-brand-new-service", "unrelated-nginx"]
        assert "cortex-brand-new-service" in discover_containers(running)

    def test_non_cortex_containers_are_not_watched(self) -> None:
        assert discover_containers(["nginx", "postgres", "traefik"]) == []

    def test_the_alerter_does_not_watch_itself(self) -> None:
        """If it dies it reads nothing, including its own logs. cortex-ogqq."""
        assert "cortex-alerter" not in discover_containers(["cortex-alerter", "cortex-gateway"])
        assert "cortex-alerter" in DENYLISTED_CONTAINERS

    def test_the_five_containers_that_were_unwatched_are_now_watched(self) -> None:
        """Regression guard on the measured gap, by name."""
        running = [
            "cortex-gateway",
            "cortex-actions-router",
            "cortex-postgres",
            "cortex-teach",
            "cortex-alerter",
        ]
        watched = discover_containers(running)
        for name in running:
            if name == "cortex-alerter":
                continue
            assert name in watched, f"{name} was unwatched before and still is"


class TestClassifierFallsThroughToNoise:
    # Real messages harvested from cortex service code; every one of these was
    # dropped silently before this change.
    @pytest.mark.parametrize(
        "message",
        [
            "ERROR Gmail batch modify failed: HTTP 429 - Too Many Requests",
            "ERROR Failed to create required label: Cortex/Uncategorized",
            "CRITICAL Missing required environment variables: POSTGRES_PASSWORD",
            "ERROR Failed to get Gmail labels for 18f2a: connection reset",
        ],
    )
    def test_an_unrecognised_error_still_alerts(self, message: str) -> None:
        assert is_error_line(message), "precondition: this looks like an error"
        result = classify("cortex-gateway", message)
        assert result is not None, "an error nobody anticipated is still an error"
        assert result.severity is Severity.WARNING

    def test_unclassified_errors_dedupe_per_message(self) -> None:
        """A service failing in a loop must be one alert, not thousands."""
        a = classify("cortex-gateway", "ERROR Failed to widget: boom")
        b = classify("cortex-gateway", "ERROR Failed to widget: boom")
        c = classify("cortex-gateway", "ERROR Something entirely different")
        assert a and b and c
        assert a.error_key == b.error_key
        assert a.error_key != c.error_key

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("ERROR LLM error processing abc: timeout", Severity.HIGH),
            # Caught by a tuned pattern at CRITICAL, which is better than the
            # WARNING fallback -- this was in my first draft of the
            # unclassified list and the classifier was already right about it.
            ("ERROR Failed to get/create label Cortex/KS: HttpError 403", Severity.CRITICAL),
        ],
    )
    def test_a_tuned_pattern_still_wins(self, message: str, expected: Severity) -> None:
        """The specific patterns are enrichment now, not a gate -- but they win."""
        result = classify("cortex-gateway", message)
        assert result is not None
        assert result.severity is expected
        assert result.title != "Unclassified Error"


class TestPostgresClientFaultsAreNotOurErrors:
    """A database rejecting bad SQL is working correctly; the sender has the bug."""

    @pytest.mark.parametrize(
        "message",
        [
            'ERROR:  column "nope_no_such_column" does not exist at character 8',
            "ERROR:  aggregate functions are not allowed in GROUP BY at character 72",
            'ERROR:  unterminated quoted identifier at or near """ at character 1',
            "ERROR:  ORDER BY position 2 is not in select list at character 40",
            "ERROR:  syntax error at or near SELECT",
        ],
    )
    def test_an_agent_typo_does_not_alert(self, message: str) -> None:
        assert is_client_sql_fault("cortex-postgres", message)
        assert classify("cortex-postgres", message) is None

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR:  could not map dynamic shared memory segment",
            "FATAL:  the database system is in recovery mode",
            "PANIC:  could not write to file pg_wal/xlogtemp: No space left on device",
        ],
    )
    def test_real_server_trouble_still_alerts(self, message: str) -> None:
        assert not is_client_sql_fault("cortex-postgres", message)
        assert classify("cortex-postgres", message) is not None

    def test_the_filter_is_scoped_to_postgres(self) -> None:
        """The same words from an application are NOT a client fault."""
        msg = 'ERROR:  column "x" does not exist'
        assert not is_client_sql_fault("cortex-gateway", msg)
        assert classify("cortex-gateway", msg) is not None


class TestPatternsMustBePrecise:
    """A tuned pattern is not gated on is_error_line(), so it must be precise.

    `\\b5\\d{2}\\b` matches any number in 500-599 anywhere in a line, so routine
    Postgres bookkeeping matched the API-server-error pattern 122 times in 24h.
    It never alerted -- postgres was unwatched and the daemon gates first -- so it
    was a landmine, not an incident.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "LOG:  checkpoint complete: wrote 571 buffers (3.5%); 0 WAL file(s) added",
            "LOG:  checkpoint starting: time, 530 buffers",
            'File "/app/capture_worker.py", line 517, in _process',
            "INFO Processed 512 emails in this batch",
        ],
    )
    def test_a_bare_500_series_integer_is_not_an_http_status(self, line: str) -> None:
        result = classify("cortex-postgres", line)
        assert result is None or result.title != "API Server Error", (
            f"a 5xx-looking integer was read as an HTTP status: {line!r}"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "ERROR HttpError 503 Service Unavailable",
            "ERROR Gmail API returned HTTP 500",
            "ERROR request failed status_code=502",
            "ERROR 504 Gateway Timeout from upstream",
        ],
    )
    def test_a_real_http_5xx_still_alerts(self, line: str) -> None:
        result = classify("cortex-gmail-sync", line)
        assert result is not None
        assert result.title == "API Server Error", f"missed a real 5xx: {line!r}"


class TestCriticalPatternsDoNotMatchRoutineTraffic:
    """A CRITICAL pattern pings the channel, so a false positive is expensive.

    Each line here is real traffic that an over-broad version of one of these
    patterns matched.
    """

    @pytest.mark.parametrize(
        "line",
        [
            # A Gmail history ID is a plain integer that routinely CONTAINS
            # "404". 9 of these in 24h of healthy Pub/Sub traffic.
            '{"event": "Notification for a@b.com, historyId=79564045", "level": "info"}',
            '{"event": "Notification for a@b.com, historyId=40412345", "level": "info"}',
            '{"event": "sync complete", "historyId": 404999, "level": "info"}',
        ],
    )
    def test_a_history_id_containing_404_is_not_an_expiry(self, line: str) -> None:
        result = classify("cortex-gmail-sync", line)
        assert result is None or result.title != "Gmail History Expired", (
            f"routine notification read as history expiry: {line[:70]!r}"
        )

    @pytest.mark.parametrize(
        "line",
        [
            # Unanchored OOM matches inside ordinary words. This one is real:
            # a Gmail label called Cortex/Automated/Zoom.
            "add_label=['Cortex/Automated/Zoom'] remove_label=None",
            "INFO joining room 12345",
            "INFO bloom filter rebuilt",
        ],
    )
    def test_a_word_containing_oom_is_not_an_oom_kill(self, line: str) -> None:
        result = classify("cortex-triage-worker", line)
        assert result is None or result.title != "Out of Memory", (
            f"ordinary word read as an OOM kill: {line[:70]!r}"
        )

    @pytest.mark.parametrize(
        ("line", "title"),
        [
            ("CRITICAL History expired, full resync required", "Gmail History Expired"),
            (
                "ERROR HttpError 404 fetching history for devon: historyId too old",
                "Gmail History Expired",
            ),
            ("ERROR MemoryError: cannot allocate 4GiB", "Out of Memory"),
            ("ERROR container exited with exit code 137", "Out of Memory"),
        ],
    )
    def test_the_real_conditions_still_alert(self, line: str, title: str) -> None:
        result = classify("cortex-gmail-sync", line)
        assert result is not None, f"missed a real condition: {line!r}"
        assert result.title == title


class TestRegexesStillMatchTheRealCondition:
    """Every anchoring fix narrows a pattern, and a narrowed pattern fails silently.

    The sibling class proves these no longer match routine traffic; on its own
    that is satisfied by a pattern matching nothing. Each case here is the real
    failure its alternation exists for.
    """

    @pytest.mark.parametrize(
        "line,expect_title",
        [
            # The three new history alternations. The OLD pattern was
            # historyId.*404, which matched any history ID containing "404" as
            # a substring; these require 404 to be an HTTP status.
            (
                "HttpError 404 when requesting history for startHistoryId 123",
                "Gmail History Expired",
            ),
            (
                "historyId 79564045 rejected: HttpError 404 Not Found",
                "Gmail History Expired",
            ),
            (
                "startHistoryId 12345 returned 404",
                "Gmail History Expired",
            ),
            # \bOOM\b and \bOut of memory\b -- word-bounded, but still present.
            ("Container killed: OOM", "Out of Memory"),
            ("Out of memory: Killed process 1234 (python)", "Out of Memory"),
            # HttpError 5\d{2} keeps the HTTP context it always had.
            ("HttpError 503 Service Unavailable", "API Server Error"),
        ],
    )
    def test_the_real_failure_still_classifies(self, line, expect_title):
        result = classify("cortex-gmail-sync", line)
        assert result is not None, f"pattern went silent on a real failure: {line!r}"
        assert result.title == expect_title


class TestStructlogLevelIsAnErrorSignal:
    """Cortex services put severity in a field, not in the message text.

    `{"event": "Missing required environment variables", "level": "error"}`
    contains no word from _ERROR_INDICATORS, so a text-only gate dropped a
    service reporting it could not start.
    """

    def test_structlog_error_level_is_an_error(self):
        line = '{"event": "Missing required environment variables: PG_PASS", "level": "error"}'
        assert is_error_line(line)

    def test_structlog_critical_level_is_an_error(self):
        assert is_error_line('{"event":"Shutting down","level":"critical"}')

    def test_structlog_warning_level_is_not(self):
        line = '{"event": "DuckDB API error for x: HTTP 404", "level": "warning"}'
        assert not is_error_line(line)

    def test_structlog_info_level_is_not(self):
        assert not is_error_line('{"event": "Processed 10 emails", "level": "info"}')

    def test_the_startup_abort_now_reaches_the_catch_all(self):
        """The whole point: it is classified, not dropped."""
        line = '{"event": "Missing required environment variables: PG_PASS", "level": "error"}'
        result = classify("cortex-parse-worker", line)
        assert result is not None
        assert result.title == "Unclassified Error"


class TestDiscoveryIsWiredIn:
    """discover_containers() is pure and tested above; this is the wiring.

    The pure function can be perfect while the daemon never calls it.
    """

    @staticmethod
    def _daemon(containers=None):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            return AlerterDaemon("https://discord.test/webhook", containers=containers)

    @staticmethod
    @staticmethod
    def _listed(*names):
        """Docker's own list payload: a leading slash, and Names is a list."""
        return [{"Names": [f"/{n}"]} for n in names]

    def test_discovery_before_docker_connect_watches_nothing(self):
        """NOT a stale hardcoded list. An alerter that cannot see Docker says so."""
        d = self._daemon()
        d.docker_client = None
        assert d._discover_watch_set() == []

    def test_docker_failure_watches_nothing(self):
        d = self._daemon()
        d.docker_client = mock.Mock()
        d.docker_client.api.containers.side_effect = RuntimeError("socket gone")
        assert d._discover_watch_set() == []

    def test_discovery_does_not_inspect_each_container(self):
        """utils-1756. `containers.list()` inspects every container it lists,
        and an inspect of one removed since the listing raises NotFound --
        which took discovery to the branch above and emptied the watch set,
        because discovery runs once. One list call, no inspect, no window.
        """
        d = self._daemon()
        d.docker_client = mock.Mock()
        d.docker_client.api.containers.return_value = self._listed("cortex-gateway")
        assert d._discover_watch_set() == ["cortex-gateway"]
        d.docker_client.containers.list.assert_not_called()

    def test_discovery_returns_cortex_containers_minus_denylist(self):
        d = self._daemon()
        d.docker_client = mock.Mock()
        d.docker_client.api.containers.return_value = self._listed(
            "cortex-gateway", "cortex-alerter", "traefik", "cortex-teach"
        )
        assert d._discover_watch_set() == ["cortex-gateway", "cortex-teach"]

    def test_a_stopped_container_is_still_in_the_watch_set(self):
        """utils-a6ba. cortex-teach died six seconds before discovery ran on
        2026-11-09 and so was never watched at all; `containers_watched` fell
        11 -> 10 in step with `tailers_streaming`, which is why the rule
        comparing them stayed clean through a dead container.
        """
        d = self._daemon()
        d.docker_client = mock.Mock()

        def listing(all=False):  # noqa: A002 -- docker-py's own parameter name
            names = ["cortex-gateway", "cortex-teach"] if all else ["cortex-gateway"]
            return self._listed(*names)

        d.docker_client.api.containers.side_effect = listing
        assert d._discover_watch_set() == ["cortex-gateway", "cortex-teach"], (
            "discovery read the RUNNING list, so a container that is down is unwatched"
        )

    def _run_once(self, daemon, discovered):
        """Drive run() to completion without booting the daemon."""
        daemon._stop_event.set()  # run() returns at the final wait()
        with (
            mock.patch.object(daemon, "_connect_docker", return_value=True),
            mock.patch.object(daemon, "_tail_container"),
            mock.patch.object(daemon, "_discover_watch_set", return_value=discovered),
            mock.patch("cortex_utils.alerter.daemon.schedule"),
        ):
            daemon.run()
        return daemon.containers

    def test_run_discovers_when_no_explicit_list(self):
        d = self._daemon()
        assert self._run_once(d, ["cortex-gateway"]) == ["cortex-gateway"]

    def test_an_explicit_container_list_is_not_discarded(self):
        """-c wins. Discovery is the DEFAULT, not an override."""
        d = self._daemon(containers=["cortex-only-this"])
        assert d._discover is False
        assert self._run_once(d, ["cortex-gateway"]) == ["cortex-only-this"]

    def test_watching_nothing_is_reported_loudly(self, capsys):
        """structlog renders to stderr, so caplog does not see this one."""
        d = self._daemon()
        self._run_once(d, [])
        assert "NOTHING" in capsys.readouterr().err


class TestTheDedupKeyCollapsesRealTraffic:
    """One fault must produce one key, however many times it fires.

    Hashing the raw line hashed structlog's per-emit timestamp, so the key was
    unique per LINE: 42 unclassified errors over 7 days gave 42 keys. With
    normalisation, 25.
    """

    @staticmethod
    def _line(event, ts="2026-09-19T01:00:00.111111Z"):
        return json.dumps(
            {
                "event": event,
                "service": "triage-worker",
                "logger": "__main__",
                "level": "error",
                "timestamp": ts,
            }
        )

    def test_the_same_fault_one_second_later_is_the_same_key(self):
        a = classify("cortex-triage-worker", self._line("Config reload failed"))
        b = classify(
            "cortex-triage-worker",
            self._line("Config reload failed", "2026-09-19T01:00:01.222222Z"),
        )
        assert a.error_key == b.error_key

    def test_the_same_fault_on_a_different_item_is_the_same_key(self):
        """Six real 'Pattern detection failed for <gmail-id>' lines are ONE fault."""
        a = classify(
            "cortex-triage-worker", self._line("Pattern detection failed for 1a06430fa5df4c3e")
        )
        b = classify(
            "cortex-triage-worker", self._line("Pattern detection failed for 1a05dcab473eb3dc")
        )
        assert a.error_key == b.error_key

    def test_postgres_clock_and_pid_do_not_split_one_fault(self):
        def mk(ts, pid):
            return (
                f"{ts} UTC [{pid}] ERROR:  duplicate key value violates "
                'unique constraint "unique_pattern"'
            )

        a = classify("cortex-postgres", mk("2026-09-14 16:23:14.923", 42))
        b = classify("cortex-postgres", mk("2026-09-14 16:24:02.117", 8891))
        assert a.error_key == b.error_key

    def test_a_loop_of_five_hundred_emissions_is_one_key(self):
        """The property the comment claims. It was 500 keys."""
        keys = {
            classify(
                "cortex-triage-worker", self._line("Worker crashed", f"2026-09-19T01:00:{n:02d}.0Z")
            ).error_key
            for n in range(60)
        }
        assert len(keys) == 1, f"a repeating fault produced {len(keys)} keys"

    def test_genuinely_different_faults_stay_apart(self):
        """Normalisation must not over-collapse into one useless bucket."""
        keys = {
            classify("cortex-triage-worker", self._line(e)).error_key
            for e in (
                "Config reload failed",
                "Gmail batch modify failed",
                "Cannot start without Docker connection",
            )
        }
        assert len(keys) == 3


class TestWarningsNeverReachTheChannel:
    """WARNING is now the path every untriaged error takes.

    If that branch ever pings Discord the alerter becomes the flood it exists to
    avoid, and adding send_embed() to it passed the whole suite.
    """

    @staticmethod
    def _daemon():
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        d.discord = mock.Mock()
        return d

    def test_an_unclassified_error_is_counted_and_not_sent(self):
        d = self._daemon()
        line = json.dumps({"event": "Config reload failed", "level": "error"})
        assert classify("cortex-x", line).severity is Severity.WARNING
        d._process_log_line("cortex-x", line)
        d.discord.send_embed.assert_not_called()
        d.discord.send.assert_not_called()

    def test_it_really_was_counted(self):
        """Otherwise 'not sent' is satisfied by doing nothing at all."""
        d = self._daemon()
        d.rate_limiter = mock.Mock()
        d._process_log_line(
            "cortex-x", json.dumps({"event": "Config reload failed", "level": "error"})
        )
        d.rate_limiter.increment_warning.assert_called_once()
        d.rate_limiter.should_alert.assert_not_called()

    def test_a_critical_still_does_reach_the_channel(self):
        """The control: WARNING silence must not be silence for everything."""
        d = self._daemon()
        line = "psycopg2.OperationalError: could not connect to server"
        assert classify("cortex-x", line).severity is Severity.CRITICAL
        d._process_log_line("cortex-x", line)
        assert d.discord.send_embed.called, "a real critical must still alert"


class TestTheDailySummaryTruncatesLoudly:
    """A cap that stops saying it capped fails toward silence."""

    @staticmethod
    def _daemon():
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        d.discord = mock.Mock()
        return d

    def _summary_of(self, n, sample="something went wrong"):
        """Drives a REAL RateLimiter.

        Stubbing it hid the defect: the message was discarded at increment_warning(),
        and a mocked limiter supplies the dict the real one failed to populate.
        """
        d = self._daemon()
        for i in range(n):
            d.rate_limiter.increment_warning(f"cortex-x:unclassified:{i:04x}", sample)
        d._send_daily_summary()
        return d.discord.send_embed.call_args.kwargs["description"]

    def test_it_caps_at_twenty_and_says_how_many_it_dropped(self):
        body = self._summary_of(51)
        assert body.count("\n- ") == 20, "the cap must hold"
        assert "... and 31 more" in body, "a silent truncation is the bug this PR is about"

    def test_a_short_summary_has_no_truncation_notice(self):
        body = self._summary_of(3)
        assert body.count("\n- ") == 3
        assert "more" not in body

    def test_an_empty_day_still_reports(self):
        """Silence and 'nothing happened' must be distinguishable."""
        d = self._daemon()
        d._send_daily_summary()
        assert "No warnings" in d.discord.send_embed.call_args.kwargs["description"]


class TestClientFaultFilterNeverSuppressesCorruption:
    """The filter drops OUR typos. It must never drop the database's distress.

    `item order invariant violated for index` is btree corruption -- the
    cortex-rj2b failure that returned 85 rows where a seqscan returned 112, and
    went undetected because nothing alerted on it. Quietening the channel by
    widening this filter would rebuild that blindness on purpose.
    """

    @pytest.mark.parametrize(
        "line",
        [
            'ERROR:  item order invariant violated for index "unique_email_mapping"',
            "ERROR:  invalid collation version change",
            'ERROR:  duplicate key value violates unique constraint "unique_pattern"',
            'ERROR:  no partition of relation "queue" found for row',
            "ERROR:  could not map dynamic shared memory segment",
            "ERROR:  refusing to run: this migration drops the constraint",
        ],
    )
    def test_the_databases_own_distress_is_never_filtered(self, line):
        assert not is_client_sql_fault("cortex-postgres", line), (
            "this is the database in trouble, not a caller's bad SQL"
        )

    @pytest.mark.parametrize(
        "line",
        [
            'ERROR:  column "foo" does not exist at character 8',
            'ERROR:  relation "nosuch" does not exist',
            'ERROR:  syntax error at or near "slect"',
            "ERROR:  unterminated quoted identifier at or near",
            "ERROR:  aggregate functions are not allowed in GROUP BY",
            "ERROR:  ORDER BY position 2 is not in select list",
            # The one alternation this PR added that had no test.
            'ERROR:  invalid input syntax for type integer: "abc"',
        ],
    )
    def test_our_own_typos_are_filtered(self, line):
        assert is_client_sql_fault("cortex-postgres", line)

    def test_only_postgres(self):
        """The filter is scoped by container; a worker saying this is our bug."""
        line = 'ERROR:  relation "nosuch" does not exist'
        assert is_client_sql_fault("cortex-postgres", line)
        assert not is_client_sql_fault("cortex-triage-worker", line)


class TestTheSummarySaysWhatFailed:
    """The summary must carry the error, not just a tally.

    It rendered `- **Unclassified:8A416200** (cortex-gateway): 2` -- container,
    count, opaque digest, no message -- because the text was discarded at
    increment_warning(). A summary nobody can act on is the silence this fixes.
    """

    @staticmethod
    def _summary(container, event, times=1):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=[container])
        d.discord = mock.Mock()
        for i in range(times):
            d._process_log_line(
                container,
                json.dumps(
                    {"event": event, "level": "error", "timestamp": f"2026-09-19T0{i}:00:00Z"}
                ),
            )
        d._send_daily_summary()
        return d.discord.send_embed.call_args.kwargs["description"]

    def test_the_message_reaches_the_summary(self):
        body = self._summary("cortex-gateway", "Missing required environment variables: PG_PASS")
        assert "Missing required environment variables: PG_PASS" in body

    def test_the_count_is_still_there(self):
        body = self._summary("cortex-gateway", "Config reload failed", times=3)
        assert "): 3" in body
        assert "Config reload failed" in body

    def test_the_digest_is_left_greppable(self):
        """.title() on a hex digest prints a string that matches no key."""
        body = self._summary("cortex-gateway", "Config reload failed")
        digest = classify(
            "cortex-gateway",
            json.dumps(
                {
                    "event": "Config reload failed",
                    "level": "error",
                    "timestamp": "2026-09-19T00:00:00Z",
                }
            ),
        ).error_key.rsplit(":", 1)[1]
        assert digest == digest.lower(), "precondition: keys are lowercase hex"
        assert digest in body, "the printed key must be the real one"

    def test_the_sample_represents_the_bucket_not_one_arrival(self):
        """Six ids collapse to one key, so the sample must be the normalised form."""
        body = self._summary(
            "cortex-triage-worker", "Pattern detection failed for 1a06430fa5df4c3e"
        )
        assert "Pattern detection failed for <>" in body


class TestDedupSourceSurvivesAnythingDockerEmits:
    """An exception here does not fail one line, it drops a container.

    _tail_container catches everything, sleeps 30s and restarts with since=now,
    so a raise inside classify silently discards whatever was emitted meanwhile.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "null",
            "[]",
            '"just a string"',
            "123456",
            '{"event": {"nested": "object"}}',
            '{"event": null}',
            '{"event": 5}',
            '{"event": "truncated by the log driver...',
            "",
            "\udcff a lone surrogate",
            "a NUL\x00byte",
            "plain text, not JSON at all",
        ],
    )
    def test_it_returns_a_string_and_does_not_raise(self, line):
        from cortex_utils.alerter.classifier import _dedup_source

        assert isinstance(_dedup_source(line), str)

    def test_a_surrogate_can_still_be_hashed(self):
        """errors='replace' on the encode, or classify() raises on real input."""
        assert classify("cortex-x", "ERROR \udcff broke") is not None

    def test_a_very_long_line_is_bounded(self):
        from cortex_utils.alerter.classifier import _dedup_source

        assert len(_dedup_source("ERROR " + "x" * 100_000)) <= 200


class TestAFailedSummaryDoesNotDeleteTheDay:
    """A summary nobody received must not clear the day it reported.

    reset_warning_counts() ran before the send and the send's result was
    discarded, so one Discord 5xx destroyed the day's warnings. The failure was
    logged to cortex-alerter -- the one container the denylist excludes.
    """

    @staticmethod
    def _daemon(delivered):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        d.discord = mock.Mock()
        d.discord.send_embed.return_value = delivered
        for i in range(5):
            d._process_log_line(
                "cortex-x",
                json.dumps(
                    {
                        "event": f"fault number {i}",
                        "level": "error",
                        "timestamp": f"2026-09-19T0{i}:00:00Z",
                    }
                ),
            )
        return d

    def test_a_failed_send_keeps_the_warnings(self):
        d = self._daemon(delivered=False)
        assert len(d.rate_limiter.get_warning_counts()) == 5
        d._send_daily_summary()
        assert len(d.rate_limiter.get_warning_counts()) == 5, (
            "a 5xx must not delete the day -- the next summary carries them instead"
        )

    def test_a_successful_send_does_clear(self):
        """The control: keeping them forever is its own bug."""
        d = self._daemon(delivered=True)
        d._send_daily_summary()
        assert d.rate_limiter.get_warning_counts() == {}

    def test_a_failed_send_is_reported(self):
        d = self._daemon(delivered=False)
        with mock.patch("cortex_utils.alerter.daemon.log") as logger:
            d._send_daily_summary()
        assert logger.error.called, "an undelivered summary must be loud somewhere"

    def test_an_empty_day_also_respects_delivery(self):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        d.discord = mock.Mock()
        d.discord.send_embed.return_value = False
        d._send_daily_summary()  # must not raise on the no-warnings path
        assert d.discord.send_embed.called


class TestTheSummaryIsActuallyScheduled:
    """A summary that never fires is the same outcome as one with no content."""

    @staticmethod
    def _run_once(daemon):
        daemon._stop_event.set()
        with (
            mock.patch.object(daemon, "_connect_docker", return_value=True),
            mock.patch.object(daemon, "_tail_container"),
            mock.patch.object(daemon, "_discover_watch_set", return_value=["cortex-x"]),
            mock.patch("cortex_utils.alerter.daemon.schedule") as sched,
        ):
            daemon.run()
        return sched

    def _daemon(self, hour=6):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon(
                "https://discord.test/hook", containers=["cortex-x"], summary_hour=hour
            )
        d.discord = mock.Mock()
        return d

    def test_it_schedules_the_summary_at_the_configured_hour(self):
        d = self._daemon(hour=6)
        sched = self._run_once(d)
        sched.every.return_value.day.at.assert_called_once_with("06:00")

    def test_the_hour_is_honoured(self):
        d = self._daemon(hour=23)
        sched = self._run_once(d)
        sched.every.return_value.day.at.assert_called_once_with("23:00")

    def test_it_schedules_the_summary_itself_not_some_other_job(self):
        """`.do(lambda: None)` shipped green before this."""
        d = self._daemon()
        sched = self._run_once(d)
        sched.every.return_value.day.at.return_value.do.assert_called_once_with(
            d._send_daily_summary
        )


class TestOneRaiseDoesNotKillTheScheduler:
    """schedule.run_pending() does not catch, and this is a daemon thread.

    One exception ended it permanently: every later summary silently unsent, the
    process still looking healthy.
    """

    def test_the_loop_survives_a_raising_job(self):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("the summary blew up")
            d._stop_event.set()

        with (
            mock.patch("cortex_utils.alerter.daemon.schedule.run_pending", side_effect=boom),
            mock.patch("cortex_utils.alerter.daemon.time.sleep"),
        ):
            d._schedule_loop()

        assert len(calls) == 2, "the loop must run again after a job raised"


class TestTheTailerDelivers:
    """Ingestion: every other stage assumes a line arrives here.

    _tail_container had no test, so ten mutations of it passed -- including
    replacing _process_log_line with `pass` and tailing no containers at all.
    The recovery cases are real: six cortex containers were recreated by a
    redeploy on 2026-09-16, which is the NotFound path.
    """

    @staticmethod
    def _daemon():
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=["cortex-x"])
        d.discord = mock.Mock()
        return d

    @staticmethod
    def _client(d, lines, raise_first=None):
        """A docker client whose SECOND tail attempt stops the loop.

        Stopping from inside logs() means the loop terminates whatever the mutant
        does, including one that removes the handler that would have ended it.
        """
        calls = []
        container = mock.Mock()

        def logs(**kw):
            calls.append(kw)
            if len(calls) >= 2:
                d._stop_event.set()
                return iter(())
            if raise_first is not None:
                raise raise_first
            return iter(lines)

        container.logs.side_effect = logs
        client = mock.Mock()
        client.containers.get.return_value = container
        # Without this, events() hands back a Mock, iterating it raises
        # TypeError, and every tailer test below passes down the error path
        # instead of the one it is about.
        client.events.side_effect = lambda **_kw: TestTheTailerDelivers._event_stream()
        client.logs_calls = calls
        return client

    def test_every_line_reaches_the_classifier_under_its_own_container_name(self):
        d = self._daemon()
        d.docker_client = self._client(
            d, [b'{"event": "boom", "level": "error"}\n', b"  second line  \n"]
        )
        seen = []
        d._process_log_line = lambda c, line: seen.append((c, line))
        with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
            d._tail_container("cortex-x")
        assert seen == [
            ("cortex-x", '{"event": "boom", "level": "error"}'),
            ("cortex-x", "second line"),
        ]

    def test_undecodable_bytes_do_not_drop_the_line(self):
        d = self._daemon()
        d.docker_client = self._client(d, [b"ERROR \xff broke\n"])
        seen = []
        d._process_log_line = lambda c, line: seen.append(line)
        with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
            d._tail_container("cortex-x")
        assert len(seen) == 1

    def test_it_follows_a_live_stream_rather_than_reading_the_backlog(self):
        d = self._daemon()
        d.docker_client = self._client(d, [])
        with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
            d._tail_container("cortex-x")
        kw = d.docker_client.logs_calls[0]
        assert kw["stream"] is True and kw["follow"] is True
        assert "since" in kw, "without since, every restart replays the whole history"

    @pytest.mark.parametrize(
        "exc",
        [
            docker.errors.NotFound("container recreated by a redeploy"),
            docker.errors.APIError("docker restarted"),
            RuntimeError("classify raised on a hostile line"),
        ],
    )
    def test_a_failure_does_not_end_the_watch(self, exc):
        d = self._daemon()
        d.docker_client = self._client(d, [b"x\n"], raise_first=exc)
        with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
            d._tail_container("cortex-x")
        assert len(d.docker_client.logs_calls) == 2, (
            f"{type(exc).__name__} left the container permanently unwatched"
        )

    def test_a_stream_that_ends_waits_for_docker_to_say_it_started(self):
        d = self._daemon()
        d.docker_client = self._client(d, [])
        with mock.patch.object(d, "_await_start") as wait:
            d._tail_container("cortex-x")
        assert wait.call_count == 2, "a stream ending must not reattach immediately"
        assert len(d.docker_client.logs_calls) == 2, "and it must reattach afterwards"

    def test_the_wait_happens_with_the_streaming_flag_already_cleared(self):
        """Or `tailers_streaming` would count a stopped container as streaming
        for the whole wait, which is the gauge utils-a6ba is about.
        """
        d = self._daemon()
        d.docker_client = self._client(d, [])
        during = []
        with mock.patch.object(
            d, "_await_start", side_effect=lambda *a, **k: during.append(dict(d._streaming))
        ):
            d._tail_container("cortex-x")
        assert during == [{"cortex-x": False}, {"cortex-x": False}]

    def test_a_failure_in_the_start_wait_does_not_end_the_watch(self):
        d = self._daemon()
        d.docker_client = self._client(d, [])
        with (
            mock.patch.object(d, "_await_start", side_effect=docker.errors.APIError("events gone")),
            mock.patch("cortex_utils.alerter.daemon.time.sleep"),
        ):
            d._tail_container("cortex-x")
        assert len(d.docker_client.logs_calls) == 2, "the container was left unwatched"

    @staticmethod
    def _event_stream(events=()):
        """docker-py returns a CancellableStream: iterable, and closeable."""
        stream = mock.MagicMock()
        stream.__iter__ = lambda _self: iter(events)
        return stream

    def test_the_wait_asks_docker_only_about_this_container_starting(self):
        d = self._daemon()
        d.docker_client = mock.Mock()
        d.docker_client.events.return_value = self._event_stream()
        with (
            mock.patch("cortex_utils.alerter.daemon.time.time", return_value=1000.0),
            mock.patch("cortex_utils.alerter.daemon.time.sleep"),
        ):
            d._await_start("cortex-x", since=900.0)
        kw = d.docker_client.events.call_args.kwargs
        assert kw["filters"] == {"container": "cortex-x", "event": "start"}
        assert kw["since"] == 900.0, (
            "the wait must start where the tailer attached, or a container that "
            "restarted while the stream was ending is waited for instead of replayed"
        )
        assert kw["until"] == 1030.0, "an unbounded wait would never reattach"

    def test_the_wait_returns_on_the_first_start_event_and_closes_the_stream(self):
        d = self._daemon()
        d.docker_client = mock.Mock()
        consumed = []

        def counting():
            for _ in range(3):
                consumed.append(1)
                yield {"Action": "start"}

        events = self._event_stream(counting())
        d.docker_client.events.return_value = events
        d._await_start("cortex-x", since=1.0)
        assert consumed == [1], "the wait must stop at the first start, not drain the stream"
        events.close.assert_called_once()

    @pytest.mark.parametrize(("returned_at", "held"), [(1000.5, 29.5), (1031.0, 0.0)])
    def test_a_wait_that_ends_early_still_holds_off_the_reattach(self, returned_at, held):
        """Pins the 30s window as a number, so changing it is deliberate."""
        d = self._daemon()
        d.docker_client = mock.Mock()
        d.docker_client.events.return_value = self._event_stream()
        clock = iter([1000.0, returned_at])
        with (
            mock.patch("cortex_utils.alerter.daemon.time.time", lambda: next(clock)),
            mock.patch("cortex_utils.alerter.daemon.time.sleep") as sleep,
        ):
            d._await_start("cortex-x", since=1.0)
        assert sleep.call_args_list == [mock.call(held)]

    def test_the_tailer_records_where_to_resume_before_it_attaches(self):
        d = self._daemon()
        d.docker_client = self._client(d, [])
        clock = iter([100.0, 200.0, 300.0, 400.0])
        with (
            mock.patch.object(d, "_await_start") as wait,
            mock.patch("cortex_utils.alerter.daemon.time.time", lambda: next(clock)),
        ):
            d._tail_container("cortex-x")
        assert wait.call_args_list[0].kwargs["since"] == 100.0

    def test_the_reconnect_resumes_from_now_and_so_drops_the_gap(self):
        """Pins a KNOWN LOSS so that changing it is deliberate.

        since=datetime.now() is inside the loop, so a reconnect resumes from now and
        whatever was emitted during the outage is gone -- ~63 lines at triage-worker's
        2.1/s. The redesign is cortex-okcx.
        """
        d = self._daemon()
        d.docker_client = self._client(d, [b"x\n"], raise_first=RuntimeError("boom"))
        with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
            d._tail_container("cortex-x")
        first, second = d.docker_client.logs_calls[0], d.docker_client.logs_calls[1]
        assert second["since"] > first["since"], (
            "the reconnect moved `since` forward, which is the documented loss"
        )


class TestConnectDocker:
    def test_a_docker_failure_is_reported_as_failure(self):
        d = TestTheTailerDelivers._daemon()
        with mock.patch(
            "cortex_utils.alerter.daemon.docker.from_env",
            side_effect=docker.errors.DockerException("no socket"),
        ):
            assert d._connect_docker() is False

    def test_run_refuses_to_half_start_without_docker(self):
        d = TestTheTailerDelivers._daemon()
        with (
            mock.patch.object(d, "_connect_docker", return_value=False),
            mock.patch.object(d, "_tail_container") as tail,
            mock.patch("cortex_utils.alerter.daemon.schedule"),
        ):
            d.run()
        assert not tail.called
        assert not d.discord.send.called


class TestRunStartsATailerPerContainer:
    """Discovery is pointless if nothing tails what it finds.

    _tail_container is patched in every run() test, so starting zero tailers
    passed: the alerter would discover, log, send its startup notice, and watch
    nothing.
    """

    @staticmethod
    def _run_with(containers, discovered=None):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=containers)
        d.discord = mock.Mock()
        d._stop_event.set()
        with (
            mock.patch.object(d, "_connect_docker", return_value=True),
            mock.patch.object(d, "_tail_container") as tail,
            mock.patch.object(d, "_discover_watch_set", return_value=discovered or []),
            mock.patch("cortex_utils.alerter.daemon.schedule"),
        ):
            d.run()
        return tail

    def test_one_tailer_per_watched_container(self):
        tail = self._run_with(["cortex-a", "cortex-b", "cortex-c"])
        assert sorted(c.args[0] for c in tail.call_args_list) == [
            "cortex-a",
            "cortex-b",
            "cortex-c",
        ]

    def test_discovered_containers_are_tailed_too(self):
        """Discovery is pointless if nothing tails what it found."""
        tail = self._run_with(None, discovered=["cortex-gateway", "cortex-teach"])
        assert sorted(c.args[0] for c in tail.call_args_list) == [
            "cortex-gateway",
            "cortex-teach",
        ]

    def test_watching_nothing_starts_nothing(self):
        tail = self._run_with(None, discovered=[])
        assert not tail.called


_CRIT = (
    b'{"event": "History expired for historyId 12345", "service": "gmail-sync",'
    b' "level": "error", "timestamp": "2026-09-19T01:00:00Z"}\n'
)


def _warn(n):
    return (
        b'{"event": "Pattern detection failed for 1a0643' + str(n).encode() + b'fa5df4c3e",'
        b' "service": "triage-worker", "level": "error",'
        b' "timestamp": "2026-09-19T0' + str(n).encode() + b':00:00Z"}\n'
    )


def _drain(container, frames, ping_critical=True):
    """A line from a fake Docker socket through every real component to Discord."""
    from cortex_utils.alerter.daemon import AlerterDaemon

    with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
        d = AlerterDaemon(
            "https://discord.test/hook", containers=[container], ping_critical=ping_critical
        )
    d.discord = mock.Mock()
    d.discord.send_embed.return_value = True
    calls = []
    box = mock.Mock()

    def logs(**kw):
        calls.append(kw)
        if len(calls) >= 2:
            d._stop_event.set()
            return iter(())
        return iter(frames)

    box.logs.side_effect = logs
    d.docker_client = mock.Mock()
    d.docker_client.containers.get.return_value = box
    with mock.patch("cortex_utils.alerter.daemon.time.sleep"):
        d._tail_container(container)
    return d


class TestOneRealLineReachesDiscord:
    """One real line, from a fake Docker socket to a Discord payload.

    Every stage had its own tests and nobody had run them together; this would
    have failed on the gate, the summary content and the ingestion defects at
    once.
    """

    def test_a_critical_line_produces_exactly_one_alert_naming_its_container(self):
        d = _drain("cortex-gmail-sync", [_CRIT])
        assert d.discord.send_embed.call_count == 1
        kw = d.discord.send_embed.call_args.kwargs
        fields = {f["name"]: f["value"] for f in kw["fields"]}
        assert fields["Container"] == "cortex-gmail-sync"
        assert "History expired" in fields["Log"]
        assert kw["title"].startswith("CRITICAL:")

    @pytest.mark.parametrize("ping_critical", [True, False])
    def test_ping_critical_reaches_the_payload(self, ping_critical):
        """`ping=True` hardcoded shipped green -- an operator's @here setting ignored."""
        d = _drain("cortex-gmail-sync", [_CRIT], ping_critical=ping_critical)
        assert d.discord.send_embed.call_args.kwargs["ping"] is ping_critical

    def test_a_warning_reaches_nobody_until_the_summary_then_says_what_failed(self):
        d = _drain("cortex-triage-worker", [_warn(1), _warn(2), _warn(3)])
        assert d.discord.send_embed.call_count == 0, "a WARNING must not ping the channel"
        assert list(d.rate_limiter.get_warning_counts().values()) == [3], (
            "three ids, one fault, one key"
        )
        d._send_daily_summary()
        desc = d.discord.send_embed.call_args.kwargs["description"]
        assert "Pattern detection failed" in desc, "the summary must say what failed"
        assert d.rate_limiter.get_warning_counts() == {}, "a delivered summary clears the day"


class TestTheTestAlertNamesWhatItWouldWatch:
    """`cortex alerter test` runs before run(), so self.containers is still empty.

    Replacing the hardcoded default with `[]` made this render an empty field,
    which Discord rejects and which tells an operator nothing.
    """

    @staticmethod
    def _fields(daemon):
        daemon.send_test_alert()
        return {f["name"]: f["value"] for f in daemon.discord.send_embed.call_args.kwargs["fields"]}

    @staticmethod
    def _daemon(containers=None):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient"):
            d = AlerterDaemon("https://discord.test/hook", containers=containers)
        d.discord = mock.Mock()
        return d

    def test_it_discovers_rather_than_reporting_an_empty_list(self):
        d = self._daemon()
        with (
            mock.patch.object(d, "_connect_docker", return_value=True),
            mock.patch.object(d, "_discover_watch_set", return_value=["cortex-a", "cortex-b"]),
        ):
            assert self._fields(d)["Containers"] == "cortex-a, cortex-b"

    def test_the_field_is_never_empty_even_when_docker_is_down(self):
        """A webhook test must still work when Docker does not."""
        d = self._daemon()
        with mock.patch.object(d, "_connect_docker", return_value=False):
            value = self._fields(d)["Containers"]
        assert value, "Discord rejects an empty field value"
        assert "none discovered" in value

    def test_an_explicit_list_is_reported_as_given(self):
        d = self._daemon(containers=["cortex-only"])
        assert self._fields(d)["Containers"] == "cortex-only"


class TestSustainedErrorsEscalate:
    """A multi-day outage must not look like a single blip.

    The decision is a pure function of four inputs, so these enumerate the
    input space rather than sampling it.
    """

    @staticmethod
    def _run(minutes_ago: int, count: int):
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import Run

        now = datetime.now()
        return Run(started=now - timedelta(minutes=minutes_ago), last=now, count=count), now

    @pytest.mark.parametrize("minutes_ago", [0, 29, 30, 31, 600])
    @pytest.mark.parametrize("count", [1, 2, 3, 4, 50])
    def test_both_bounds_must_be_met(self, minutes_ago, count):
        """Duration alone and count alone each escalate the wrong thing.

        Duration alone fires on one error seen once an hour ago and never
        again. Count alone fires on a three-line burst inside one second, which
        is one event.
        """
        from cortex_utils.alerter.rate_limiter import is_sustained

        run, now = self._run(minutes_ago, count)
        expected = minutes_ago >= 30 and count >= 3
        assert is_sustained(run, now, 30, 3) is expected

    def test_no_run_is_not_sustained(self):
        from datetime import datetime

        from cortex_utils.alerter.rate_limiter import is_sustained

        assert is_sustained(None, datetime.now(), 30, 3) is False

    def test_a_quiet_gap_starts_a_new_run(self):
        """Otherwise a key that fired in July and again in November reads as
        having been sustained for four months."""
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter

        rl = RateLimiter()
        t0 = datetime(2026, 7, 1, 12, 0, 0)
        rl.note_occurrence("k", now=t0, run_gap_minutes=60)
        far_later = t0 + timedelta(days=120)
        run = rl.note_occurrence("k", now=far_later, run_gap_minutes=60)
        assert run.count == 1
        assert run.started == far_later

    def test_occurrences_inside_the_gap_extend_the_run(self):
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter

        rl = RateLimiter()
        t0 = datetime(2026, 7, 1, 12, 0, 0)
        for i in range(4):
            run = rl.note_occurrence("k", now=t0 + timedelta(minutes=20 * i), run_gap_minutes=60)
        assert run.count == 4
        assert run.started == t0

    def test_the_cooldown_does_not_hide_occurrences_from_the_run(self):
        """A long run accumulates every occurrence, which is what makes it
        distinguishable from a blip."""
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter, is_sustained

        rl = RateLimiter()
        t0 = datetime(2026, 7, 24, 15, 20, 0)
        for i in range(10):
            run = rl.note_occurrence(
                "LLM Error", now=t0 + timedelta(minutes=30 * i), run_gap_minutes=60
            )
        assert run.count == 10
        assert is_sustained(run, t0 + timedelta(minutes=270), 30, 3) is True

    def test_the_escalation_defaults_are_in_a_useful_band(self):
        """The defaults are the whole behaviour for the one live consumer.

        The deployed alerter passes neither flag, so whatever these say is what
        production does. A 300-minute default means a 5-hour outage still has
        not pinged; a 30-occurrence default means a service failing slowly
        never escalates at all.
        """
        from cortex_utils.alerter.rate_limiter import (
            DEFAULT_ESCALATE_AFTER_MINUTES,
            DEFAULT_ESCALATE_AFTER_OCCURRENCES,
        )

        assert 5 <= DEFAULT_ESCALATE_AFTER_MINUTES <= 60, (
            f"{DEFAULT_ESCALATE_AFTER_MINUTES} minutes is outside the band where "
            "escalation is both prompt enough to matter and slow enough not to "
            "fire on a blip"
        )
        assert 2 <= DEFAULT_ESCALATE_AFTER_OCCURRENCES <= 10, (
            f"{DEFAULT_ESCALATE_AFTER_OCCURRENCES} occurrences is outside the band "
            "where the floor excludes a burst without excluding a slow failure"
        )

    def test_the_run_gap_boundary_is_exclusive(self):
        """A quiet period of exactly the gap CONTINUES the run; one minute more
        starts a new one. Pins `>` rather than `>=`."""
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter

        t0 = datetime(2026, 7, 1, 12, 0, 0)
        rl = RateLimiter()
        rl.note_occurrence("k", now=t0, run_gap_minutes=60)
        exactly = rl.note_occurrence("k", now=t0 + timedelta(minutes=60), run_gap_minutes=60)
        assert exactly.count == 2, "a gap of exactly the limit continues the run"

        rl2 = RateLimiter()
        rl2.note_occurrence("k", now=t0, run_gap_minutes=60)
        over = rl2.note_occurrence("k", now=t0 + timedelta(minutes=61), run_gap_minutes=60)
        assert over.count == 1, "one minute past the limit starts a new run"

    def test_the_default_run_gap_is_used_when_none_is_passed(self):
        """Exercises the default itself, which every other test overrides."""
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import (
            DEFAULT_RUN_GAP_MINUTES,
            RateLimiter,
        )

        assert 10 <= DEFAULT_RUN_GAP_MINUTES <= 240, (
            f"a run gap of {DEFAULT_RUN_GAP_MINUTES} minutes is not a plausible "
            "quiet period for an outage"
        )
        t0 = datetime(2026, 7, 1, 12, 0, 0)
        rl = RateLimiter()
        rl.note_occurrence("k", now=t0)
        inside = rl.note_occurrence("k", now=t0 + timedelta(minutes=DEFAULT_RUN_GAP_MINUTES - 1))
        assert inside.count == 2, "inside the default gap must extend the run"
        rl2 = RateLimiter()
        rl2.note_occurrence("k", now=t0)
        outside = rl2.note_occurrence("k", now=t0 + timedelta(minutes=DEFAULT_RUN_GAP_MINUTES + 1))
        assert outside.count == 1, "outside the default gap must start a new run"

    def test_an_escalated_run_never_escalates_twice(self):
        """One ping per outage, and the mechanism that achieves it.

        Forgetting the run instead re-escalates every escalate_after_minutes
        for as long as the outage lasts -- 3 pings in 12 occurrences when
        measured.
        """
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter, is_sustained

        rl = RateLimiter()
        t0 = datetime(2026, 7, 24, 15, 20, 0)
        for i in range(5):
            run = rl.note_occurrence("k", now=t0 + timedelta(minutes=15 * i), run_gap_minutes=60)
        assert is_sustained(run, t0 + timedelta(minutes=60), 30, 3) is True
        rl.mark_escalated("k")
        # The outage continues; it must not ping again.
        for i in range(5, 20):
            run = rl.note_occurrence("k", now=t0 + timedelta(minutes=15 * i), run_gap_minutes=60)
            assert run.escalated is True
            assert is_sustained(run, t0 + timedelta(minutes=15 * i), 30, 3) is False

    def test_marking_escalated_preserves_the_run(self):
        """It sets one flag and changes nothing else.

        `started` feeds the "failing for N minutes" line in the alert and
        `count` the occurrence count, so resetting either would misreport the
        outage.
        """
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter

        rl = RateLimiter()
        t0 = datetime(2026, 7, 24, 15, 20, 0)
        for i in range(4):
            before = rl.note_occurrence("k", now=t0 + timedelta(minutes=15 * i))
        rl.mark_escalated("k")
        after = rl.runs["k"]
        assert after.started == before.started
        assert after.last == before.last
        assert after.count == before.count
        assert after.escalated is True

    def test_a_new_run_after_a_quiet_gap_can_escalate_again(self):
        """A genuinely new outage is a new event and must ping."""
        from datetime import datetime, timedelta

        from cortex_utils.alerter.rate_limiter import RateLimiter, is_sustained

        rl = RateLimiter()
        t0 = datetime(2026, 7, 24, 15, 20, 0)
        for i in range(4):
            rl.note_occurrence("k", now=t0 + timedelta(minutes=15 * i), run_gap_minutes=60)
        rl.mark_escalated("k")
        later = t0 + timedelta(days=3)
        for i in range(4):
            run = rl.note_occurrence("k", now=later + timedelta(minutes=15 * i), run_gap_minutes=60)
        assert run.escalated is False, "a fresh run must not inherit the escalated flag"
        assert is_sustained(run, later + timedelta(minutes=60), 30, 3) is True

    def test_a_sustained_high_error_pings_and_a_blip_does_not(self):
        """Drives the real daemon entry point, asserting on what reaches
        Discord rather than on internal state."""
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient") as dc:
            d = AlerterDaemon(
                "https://discord.test/hook",
                containers=["cortex-triage-worker"],
                escalate_after_minutes=0,  # disabled first, to get the blip baseline
            )
            line = "ERROR Ollama error: Server disconnected without sending a response"
            d._process_log_line("cortex-triage-worker", line)
            pings = [c.kwargs.get("ping") for c in dc.return_value.send_embed.call_args_list]
            assert pings, "a HIGH error should still post something"
            assert all(p is False for p in pings), f"a blip must not ping: {pings}"

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient") as dc:
            d = AlerterDaemon(
                "https://discord.test/hook",
                containers=["cortex-triage-worker"],
                escalate_after_minutes=30,
                escalate_after_occurrences=3,
            )
            # Backdate a run that has already been going for two hours.
            from datetime import datetime, timedelta

            from cortex_utils.alerter.rate_limiter import Run

            key = None
            from cortex_utils.alerter.classifier import classify

            key = classify("cortex-triage-worker", line).error_key
            now = datetime.now()
            d.rate_limiter.runs[key] = Run(started=now - timedelta(hours=2), last=now, count=9)
            d._process_log_line("cortex-triage-worker", line)
            calls = dc.return_value.send_embed.call_args_list
            assert calls, "a sustained error must post"
            assert any(c.kwargs.get("ping") is True for c in calls), (
                f"a sustained outage must ping: {[c.kwargs.get('ping') for c in calls]}"
            )
            titles = [c.kwargs.get("title", "") for c in calls]
            assert any("CRITICAL" in t and "sustained" in t for t in titles), titles

    def test_the_llm_description_does_not_claim_a_rules_fallback(self):
        """On LLM error the job raises, retries and dead-letters with no
        classification row, so the mail is unlabelled, not rules-classified."""
        c = classify("cortex-triage-worker", "ERROR Ollama error: No route to host")
        assert c is not None
        assert c.severity == Severity.HIGH
        assert "fall back to rules" not in c.description
        assert "dead-lettered" in c.description.lower()


class _Clock:
    """A controllable `datetime.now()` for the two modules that read the wall clock.

    The daemon and the rate limiter both call `datetime.now()` internally, so
    driving the daemon across simulated time means patching the `datetime` name
    in both. Patching only `rate_limiter`'s would leave the daemon's own
    `now` real, and the two would disagree by however long the test took.
    """

    def __init__(self, start):
        self.t = start

    def advance(self, **kw):
        from datetime import timedelta

        self.t += timedelta(**kw)

    def now(self, tz=None):
        return self.t


class TestTheDaemonIsWiredToTheRunTracker:
    """The wiring, which the unit tests above do not cover.

    These drive the real `_process_log_line` over simulated time, with nothing
    backdated by hand.
    """

    LINE = "ERROR Ollama error: Server disconnected without sending a response"

    @staticmethod
    def _daemon(clock, **kw):
        from cortex_utils.alerter.daemon import AlerterDaemon

        with mock.patch("cortex_utils.alerter.daemon.DiscordClient") as dc:
            d = AlerterDaemon(
                "https://discord.test/hook", containers=["cortex-triage-worker"], **kw
            )
            return d, dc.return_value

    def _feed(self, d, clock, times, step_minutes):
        """Feed the same line `times` times, `step_minutes` apart."""
        with (
            mock.patch("cortex_utils.alerter.daemon.datetime", clock),
            mock.patch("cortex_utils.alerter.rate_limiter.datetime", clock),
        ):
            for _ in range(times):
                d._process_log_line("cortex-triage-worker", self.LINE)
                clock.advance(minutes=step_minutes)

    @staticmethod
    def _pings(client):
        return [c.kwargs.get("ping") for c in client.send_embed.call_args_list]

    @staticmethod
    def _titles(client):
        return [c.kwargs.get("title", "") for c in client.send_embed.call_args_list]

    def test_a_sustained_outage_pings_exactly_once(self):
        """Pins `note_occurrence`, `mark_escalated` and the elif, all at once.

        Without `note_occurrence` no run accumulates and nothing ever pings.
        Without `mark_escalated` every line past the threshold pings again.
        With `if` instead of `elif` the escalation also posts the old Warning
        embed.
        """
        from datetime import datetime

        clock = _Clock(datetime(2026, 7, 24, 15, 20, 0))
        d, client = self._daemon(clock, escalate_after_minutes=30, escalate_after_occurrences=3)
        # 12 occurrences over 2h, 11 minutes apart: past the 10-minute cooldown
        # on the LLM Error pattern, so each one is also eligible to post.
        self._feed(d, clock, times=12, step_minutes=11)

        pings = self._pings(client)
        assert pings.count(True) == 1, (
            f"a sustained outage must ping exactly once, got {pings.count(True)}: {pings}"
        )
        titles = self._titles(client)
        sustained = [x for x in titles if "sustained" in x]
        assert len(sustained) == 1, f"exactly one sustained embed expected: {titles}"
        # ONE embed per occurrence, never two. 12 occurrences 11 minutes apart
        # all clear the 10-minute cooldown, so each posts exactly once; a 13th
        # embed means the escalating occurrence ALSO posted the old Warning,
        # which is what `if` instead of `elif` does.
        assert len(titles) == 12, f"expected one embed per occurrence, got {len(titles)}: {titles}"

    def test_escalation_waits_for_both_bounds_in_the_daemon(self):
        """Three occurrences inside one minute must not ping.

        Pins the duration bound at the daemon, not just in `is_sustained`.
        """
        from datetime import datetime

        clock = _Clock(datetime(2026, 7, 24, 15, 20, 0))
        d, client = self._daemon(clock, escalate_after_minutes=30, escalate_after_occurrences=3)
        self._feed(d, clock, times=5, step_minutes=0)
        assert True not in self._pings(client), (
            f"a burst inside one instant is one event, not an outage: {self._pings(client)}"
        )

    def test_escalate_after_zero_disables_escalation(self):
        """Pins the `> 0` guard. `or` instead of `and`, or `>= 0` instead of
        `> 0`, both ship escalation ON when it was asked to be off."""
        from datetime import datetime

        clock = _Clock(datetime(2026, 7, 24, 15, 20, 0))
        d, client = self._daemon(clock, escalate_after_minutes=0, escalate_after_occurrences=3)
        self._feed(d, clock, times=12, step_minutes=11)
        pings = self._pings(client)
        assert pings, "disabling escalation must not disable alerting"
        assert True not in pings, f"--escalate-after 0 must never ping: {pings}"

    def test_a_quiet_gap_resets_the_run_in_the_daemon(self):
        """Two occurrences either side of a long quiet period are two events.

        Pins that the daemon's calls really do go through the gap logic, and
        not merely that `note_occurrence` has it.
        """
        from datetime import datetime

        clock = _Clock(datetime(2026, 7, 24, 15, 20, 0))
        d, client = self._daemon(clock, escalate_after_minutes=30, escalate_after_occurrences=3)
        self._feed(d, clock, times=2, step_minutes=11)
        clock.advance(hours=6)
        self._feed(d, clock, times=2, step_minutes=11)
        assert True not in self._pings(client), (
            "two short bursts six hours apart are not a sustained outage"
        )


class TestTheEscalationConfigReachesTheDaemon:
    """The CLI and `run_alerter` plumbing: the option-to-kwarg mapping is the
    only thing that makes the flag work."""

    def test_run_alerter_forwards_both_knobs(self):
        from cortex_utils.alerter import daemon as daemon_mod

        seen = {}

        class _Spy:
            def __init__(self, *a, **kw):
                seen.update(kw)

            def run(self):
                pass

        with mock.patch.object(daemon_mod, "AlerterDaemon", _Spy):
            daemon_mod.run_alerter(
                webhook_url="https://discord.test/hook",
                escalate_after_minutes=7,
                escalate_after_occurrences=11,
            )
        assert seen.get("escalate_after_minutes") == 7, seen
        assert seen.get("escalate_after_occurrences") == 11, seen

    def test_the_cli_options_reach_the_daemon(self):
        from click.testing import CliRunner

        from cortex_utils import cli as cli_mod

        seen = {}

        def _spy(**kw):
            seen.update(kw)

        with mock.patch.object(cli_mod, "run_alerter", _spy):
            res = CliRunner().invoke(
                cli_mod.main,
                ["alerter", "run", "--escalate-after", "9", "--escalate-occurrences", "4"],
                env={"DISCORD_WEBHOOK_URL": "https://discord.test/hook"},
            )
        assert res.exit_code == 0, res.output
        assert seen.get("escalate_after_minutes") == 9, seen
        assert seen.get("escalate_after_occurrences") == 4, seen

    def test_the_cli_defaults_are_not_zero(self):
        """A default of 0 would ship escalation OFF.

        That is the failure this whole PR exists to prevent, arriving through
        the config rather than the logic.
        """
        from click.testing import CliRunner

        from cortex_utils import cli as cli_mod

        seen = {}

        with mock.patch.object(cli_mod, "run_alerter", lambda **kw: seen.update(kw)):
            res = CliRunner().invoke(
                cli_mod.main,
                ["alerter", "run"],
                env={"DISCORD_WEBHOOK_URL": "https://discord.test/hook"},
            )
        assert res.exit_code == 0, res.output
        # The CLI default must BE the constant, not merely non-zero. The band
        # test below guards the constants; production goes through this CLI, so
        # a hardcoded default here would leave that band guarding nothing.
        from cortex_utils.alerter.rate_limiter import (
            DEFAULT_ESCALATE_AFTER_MINUTES,
            DEFAULT_ESCALATE_AFTER_OCCURRENCES,
        )

        assert seen.get("escalate_after_minutes") == DEFAULT_ESCALATE_AFTER_MINUTES, seen
        assert seen.get("escalate_after_occurrences") == DEFAULT_ESCALATE_AFTER_OCCURRENCES, seen
