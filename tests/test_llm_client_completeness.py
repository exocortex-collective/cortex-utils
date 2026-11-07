"""A truncated model response must not look like a complete one.

The client read every field of the parsed JSON with a default, so a truncated
response produced a well-formed, plausible result: ("unknown", 0.5, ""). The
shape errors were already loud; those three defaults were not. Two of them are
now required -- `category` and `confidence`. `reasoning` still defaults, which
test_a_null_reasoning_is_coerced_not_raised pins deliberately.

The consumer is triage's `engine/matcher.py` -- the only caller of
classify/classify_with_extraction in the estate. It does not threshold the
confidence, it records it, so the damage is a fabricated row rather than a
wrong branch. reflex parses its own JSON and has its own guards.

gemma4:26b is a reasoning model: it emits a `thinking` field before `content`,
so under a tight token budget the whole budget goes on thinking and `content`
comes back as an EMPTY STRING with HTTP 200 and done_reason "length". Measured
in the foodcam project, not re-measured here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import httpx
import pytest

from cortex_utils.llm.client import (
    LLMClient,
    LLMError,
    LLMHTTPError,
    LLMTruncatedError,
)


class TestTruncationIsNotCompletion:
    """Both API shapes report it, and neither used to be read."""

    @pytest.mark.parametrize(
        "payload,shape",
        [
            ({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, "openai"),
            ({"done_reason": "length", "response": ""}, "ollama native"),
            (
                {"choices": [{"message": {"content": "half an ans"}, "finish_reason": "length"}]},
                "openai, partial content",
            ),
        ],
    )
    def test_a_budget_stop_raises(self, payload, shape):
        with pytest.raises(LLMTruncatedError):
            LLMClient._reject_if_truncated(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            {"done_reason": "stop", "response": "ok"},
            {"choices": [{"message": {"content": "ok"}}]},
            {},
        ],
    )
    def test_a_normal_stop_passes_through(self, payload):
        LLMClient._reject_if_truncated(payload)

    def test_it_is_not_retryable_and_says_so(self):
        """A retry re-spends the same budget on the same prompt.

        The generator is not the variable -- the budget is -- so the attempt
        counter is the only thing a retry moves. The caller has to change an
        input (raise max_tokens, think=False, another model) instead.

        NOTHING BRANCHES ON THIS FLAG YET. triage's worker catches the base
        LLMError and retries three times regardless (worker.py:1372 ->
        fail_or_retry). Wiring it up is cortex-hox9; until then the flag is
        machine-readable documentation, and the message carries the advice.
        """
        assert LLMTruncatedError.retryable is False
        assert LLMError.retryable is True, "the base class stays retryable"
        assert issubclass(LLMTruncatedError, LLMError), "existing handlers still catch it"


class TestAMissingFieldIsNotAnAnswer:
    """Presence, not value. An explicit 'unknown' is a judgement; absence is not."""

    def test_a_missing_confidence_raises_rather_than_becoming_point_five(self):
        with pytest.raises(LLMError, match="confidence"):
            LLMClient._required({"category": "admin"}, "confidence")

    def test_a_missing_category_raises_rather_than_becoming_unknown(self):
        with pytest.raises(LLMError, match="category"):
            LLMClient._required({"confidence": 0.9}, "category")

    @pytest.mark.parametrize("value", ["unknown", "", 0, 0.0, False, None])
    def test_an_explicit_value_is_honoured_including_falsey_ones(self, value):
        """The check is present-vs-absent. A model may legitimately say 'unknown'."""
        assert LLMClient._required({"category": value}, "category") == value


def _client_returning(body: dict, native_body: dict | None = None) -> LLMClient:
    """A real LLMClient whose HTTP layer returns `body`.

    Drives the actual public method rather than the helper, because a correct
    helper is worthless if the call site stops calling it -- three mutants that
    reverted the call sites survived a suite that tested only the helpers.
    """
    c = LLMClient("http://llm.test")
    calls: list[dict] = []

    def post(url: str, json: dict | None = None, **kw: object) -> httpx.Response:
        calls.append({"url": url, "json": json})
        payload = native_body if url.endswith("/api/generate") and native_body else body
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    c.client = mock.Mock()
    c.client.post.side_effect = post
    c.posted = calls  # type: ignore[attr-defined]
    return c


def _wrap(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class TestTheCallSitesActuallyRefuse:
    """classify() must not invent a field. Its sibling is covered below."""

    @pytest.mark.parametrize("missing", ["confidence", "category"])
    def test_classify_refuses_a_response_missing_a_required_field(self, missing):
        full = {"category": "admin", "confidence": 0.9, "reasoning": "because"}
        del full[missing]
        c = _client_returning(_wrap(json.dumps(full)))
        # match on _required's OWN message. `match=missing` was satisfied by a
        # bare KeyError re-raised as LLMError("LLM error: 'confidence'"), so it
        # could not tell the guard from no guard at all.
        with pytest.raises(LLMError, match=f"has no {missing!r}"):
            c.classify("prompt", "some-model")

    def test_classify_accepts_a_complete_response(self):
        """The control: refusing everything would also pass the tests above."""
        c = _client_returning(
            _wrap(json.dumps({"category": "admin", "confidence": 0.9, "reasoning": "r"}))
        )
        assert c.classify("prompt", "some-model") == ("admin", 0.9, "r")

    def test_classify_refuses_a_non_numeric_confidence(self):
        c = _client_returning(
            _wrap(json.dumps({"category": "admin", "confidence": "high", "reasoning": "r"}))
        )
        with pytest.raises(LLMError, match="confidence"):
            c.classify("prompt", "some-model")

    def test_a_truncated_response_raises_from_the_public_method(self):
        """Partial content, not empty.

        Empty content now triggers the native think=False recovery FIRST -- see
        the ordering test below. An earlier version used empty content and
        passed only because the guard ran before the recovery, which made the
        recovery unreachable for the one case it was written for.
        """
        c = _client_returning(
            {"choices": [{"message": {"content": '{"category": "adm'}, "finish_reason": "length"}]}
        )
        with pytest.raises(LLMTruncatedError):
            c.classify("prompt", "some-model")

    def test_an_empty_truncated_response_still_reaches_the_recovery(self):
        """The ordering itself, pinned.

        A reasoning model spends its budget on `thinking` and returns EMPTY
        content with done_reason "length" -- both the fallback's trigger and the
        guard's. Judging before recovering cancelled the fix.
        """
        c = _client_returning(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            native_body={
                "response": json.dumps({"category": "admin", "confidence": 0.9, "reasoning": "r"}),
                "done_reason": "stop",
            },
        )
        assert c.classify("prompt", "some-model") == ("admin", 0.9, "r")
        assert any(p["url"].endswith("/api/generate") for p in c.posted), (
            "the think=False recovery must run for an empty truncated response"
        )


class TestTheNativeFallbackDisablesThinking:
    """A reasoning model must not silently spend the budget on thinking."""

    def test_the_fallback_sends_think_false(self):
        # empty content on the chat API triggers the native fallback
        c = _client_returning(
            _wrap(""),
            native_body={
                "response": json.dumps({"category": "admin", "confidence": 0.5, "reasoning": "r"}),
                "done_reason": "stop",
            },
        )
        c.classify("prompt", "some-model")
        native = [p for p in c.posted if p["url"].endswith("/api/generate")]
        assert native, "the native fallback was never reached"
        assert native[0]["json"].get("think") is False, (
            "think must be explicitly disabled, or a reasoning model returns empty content"
        )


def test_the_truncation_error_is_importable_by_name():
    """A caller cannot catch what it cannot import.

    LLMTruncatedError existed in client.py but `__init__.py` never re-exported
    it, so `from cortex_utils.llm import LLMTruncatedError` raised ImportError.
    The guard existed and could not be reached.

    `__all__` governs `import *` only and does NOT affect an explicit-name
    import -- an earlier version of this docstring said it did, which would
    teach the next reader a false rule about Python. The re-export is what
    binds the name; this asserts both.
    """
    import cortex_utils.llm as pkg
    from cortex_utils.llm import LLMTruncatedError as Imported

    assert "LLMTruncatedError" in pkg.__all__
    assert Imported is LLMTruncatedError


class TestTheOtherCallSiteRefusesToo:
    """classify_with_extraction() is the LIVE path, not the fallback.

    triage's matcher calls it whenever a rule sets `llm.extract` and only falls
    back to classify() otherwise -- so the method the first version of this file
    tested was the fallback, and the one it left uncovered was the feature.
    Reverting its two _required calls shipped ('unknown', 0.5, 'r', {}) green:
    the exact tuple this ticket exists to eliminate.
    """

    @pytest.mark.parametrize("missing", ["confidence", "category"])
    def test_it_refuses_a_response_missing_a_required_field(self, missing):
        full = {"category": "admin", "confidence": 0.9, "reasoning": "r"}
        del full[missing]
        c = _client_returning(_wrap(json.dumps(full)))
        with pytest.raises(LLMError, match=f"has no {missing!r}"):
            c.classify_with_extraction("prompt", "some-model", ["amount"])

    def test_it_refuses_a_non_numeric_confidence(self):
        """The sibling of the classify() case, on the path triage actually takes."""
        c = _client_returning(
            _wrap(json.dumps({"category": "admin", "confidence": "high", "reasoning": "r"}))
        )
        with pytest.raises(LLMError, match="confidence"):
            c.classify_with_extraction("prompt", "some-model", ["amount"])

    def test_it_accepts_a_complete_response_and_strips_extracted(self):
        """The control, which also covers the extracted filter loop."""
        c = _client_returning(
            _wrap(
                json.dumps(
                    {
                        "category": "admin",
                        "confidence": 0.9,
                        "reasoning": "r",
                        "extracted": {"amount": " 12 ", "unwanted": "x"},
                    }
                )
            )
        )
        assert c.classify_with_extraction("prompt", "some-model", ["amount"]) == (
            "admin",
            0.9,
            "r",
            {"amount": "12"},
        )


def test_a_truncated_native_fallback_raises():
    """The PR's headline fix, which had no test.

    The native wrap used to drop `done_reason`, so a truncated native response
    arrived looking complete. Deleting the carry-through, or the reject that
    follows it, was green: classify() returned ('admin', 0.9, '') instead of
    raising. The only other fallback test sets done_reason "stop", so it never
    touches this.
    """
    c = _client_returning(
        _wrap(""),  # empty chat content triggers the native retry
        native_body={
            "response": json.dumps({"category": "admin", "confidence": 0.9, "reasoning": ""}),
            "done_reason": "length",
        },
    )
    with pytest.raises(LLMTruncatedError):
        c.classify("prompt", "some-model")


class TestEveryPublicMethodAgreesAboutTruncation:
    """The guard lives in _post_completion, so it governs six public methods.

    A mutant that moved it out of the shared helper and into classify() alone
    survived the whole suite -- only classify was asserted, so the shared
    POSITION was unpinned even though the behaviour was right.

    Two methods opt OUT, and the rule is which direction a truncated body can
    push the answer. check_intent and check_email_intent return
    `answer == "yes"`, so truncation can only ever yield False -- the same
    negative they already return when the model declines. Everything else
    either parses structure or hands back a value that gets used, where a
    partial body is a WRONG answer rather than a negative one.

    Measured on the active config 2026-10-07: 3 live rules reach check_intent,
    5 check_email_intent, 13 categorize_email, and 0 reach classify or
    classify_with_extraction -- so the two methods this ticket was written
    about have no live caller, and the guard's whole live effect is on the
    other three. That is why the opt-out is not a detail.
    """

    TRUNCATED = {
        "choices": [{"message": {"content": "yes, because the sender"}, "finish_reason": "length"}]
    }

    def _call(self, name):
        c = _client_returning(self.TRUNCATED)
        return {
            "check_intent": lambda: c.check_intent("s", "p", "m"),
            "check_email_intent": lambda: c.check_email_intent("f", "s", "b", "p", "m"),
            "categorize_email": lambda: c.categorize_email(
                "f", "s", "b", "p", "m", ["yes", "sales"]
            ),
            "extract_value": lambda: c.extract_value("f", "s", "b", "p", "m"),
            "classify": lambda: c.classify("p", "m"),
            "classify_with_extraction": lambda: c.classify_with_extraction("p", "m", ["f"]),
        }[name]()

    @pytest.mark.parametrize("method", ["categorize_email", "classify", "classify_with_extraction"])
    def test_a_structured_reader_refuses_a_truncated_body(self, method):
        """categorize_email is the non-obvious one, and it belongs here.

        It falls back to a substring match over the category list, sorted
        longest-first. A body cut from "sales_followup" to "sales" matches the
        WRONG category and returns it as a confident answer -- which is this
        ticket's defect with a different payload, so it keeps the guard.
        """
        with pytest.raises(LLMTruncatedError):
            self._call(method)

    def test_extraction_degrades_to_no_value_rather_than_a_partial_one(self):
        """extract_value converts LLMError to None by documented design.

        It must not return the partial string: the value becomes a rule
        variable, so half an answer is substituted into a live rule.
        """
        assert self._call("extract_value") is None

    @pytest.mark.parametrize("method", ["check_intent", "check_email_intent"])
    def test_a_yes_no_check_tolerates_truncation(self, method):
        """Deliberately NOT raising. Reverting this breaks 8 live rules.

        A 10-token budget on a yes/no question truncates as a matter of
        course; raising would dead-letter the job and leave the mail
        unlabelled, which is worse than the False these already return.
        """
        assert self._call(method) is False

    def test_the_opt_out_is_narrow(self):
        """The yes/no pair tolerates truncation; it does not ignore it.

        A truncated body whose answer IS "yes" still reads as yes -- the
        method is tolerant because its match is robust, not because it stopped
        looking.
        """
        c = _client_returning(
            {"choices": [{"message": {"content": "yes"}, "finish_reason": "length"}]}
        )
        assert c.check_intent("s", "p", "m") is True


class TestTruncationBeatsAnUnreadableShape:
    """A body can be both truncated and unparseable; truncation is the cause.

    The content read used to run first, so a response whose budget went on
    `thinking` and came back with content null -- the exact case in the module
    docstring -- was reported as a retryable shape error.
    """

    @pytest.mark.parametrize(
        "payload,what",
        [
            (
                {"choices": [{"message": {"content": None}, "finish_reason": "length"}]},
                "content null",
            ),
            ({"choices": [], "done_reason": "length"}, "choices cut off entirely"),
        ],
    )
    def test_it_is_reported_as_truncation_not_as_a_bad_shape(self, payload, what):
        c = _client_returning(payload)
        with pytest.raises(LLMTruncatedError):
            c.classify("p", "m")

    def test_an_unreadable_shape_that_is_not_truncated_still_raises_plainly(self):
        """The control: truncation must not become the explanation for everything."""
        c = _client_returning({"choices": [{"message": {"content": None}}]})
        with pytest.raises(LLMError) as e:
            c.classify("p", "m")
        assert not isinstance(e.value, LLMTruncatedError)


class TestTheReturnTypeIsEnforcedNotJustAnnotated:
    """`classify` is annotated `-> tuple[str, float, str]`; nothing enforced it.

    cortex-to6h. Found by applying docs/agent-isolation.md item 3's assertion
    -- a TYPE check must precede a MEMBERSHIP test -- to this client, after the
    same assertion caught a defect in reflex. An annotation is a claim, and
    three services read `category` on the strength of it.

    The downstream test below is the point of this class. Asserting only that
    the client raises would leave the reason unstated, and the reason is that
    the consumer's membership test is what actually blows up:

        triage/engine/matcher.py   `if category in rule.routes`
        rule.routes                dict[str, Action]
        -> TypeError: unhashable type: 'list'

    past matcher.py's `except VariableError` to the worker's broad
    `except Exception` -> three attempts -> dead letter. The email is never
    classified, so it is never labelled, so the workflow never dispatches.
    """

    @pytest.mark.parametrize("category", [["Newsletter"], {"a": 1}, 3, None, True, 1.5])
    def test_a_non_string_category_is_refused(self, category):
        c = _client_returning(
            _wrap(json.dumps({"category": category, "confidence": 0.9, "reasoning": "r"}))
        )
        with pytest.raises(LLMError, match="not a string"):
            c.classify("p", "m")

    @pytest.mark.parametrize("category", [["Newsletter"], {"a": 1}, 3, None, True, 1.5])
    def test_the_extraction_variant_refuses_it_too(self, category):
        """Both call sites, because fixing one is how this came back."""
        c = _client_returning(
            _wrap(json.dumps({"category": category, "confidence": 0.9, "reasoning": "r"}))
        )
        with pytest.raises(LLMError, match="not a string"):
            c.classify_with_extraction("p", "m", ["f"])

    def test_what_the_consumer_would_otherwise_do_with_it(self):
        """Names the consequence, so the guard's reason survives a refactor.

        If the client ever hands a list back again, this is what happens two
        layers away -- not a refusal, a TypeError from the membership test the
        code uses to decide the category is unknown.
        """
        routes = {"Newsletter": "some-action"}
        assert "Newsletter" in routes  # the intended path
        assert "Unknown" not in routes  # the intended refusal
        with pytest.raises(TypeError, match="unhashable"):
            ["Newsletter"] in routes  # noqa: B015 -- the raise IS the assertion

    def test_an_explicit_unknown_string_is_still_honoured(self):
        """The control: this guard is about TYPE, not about rejecting values.

        `classify`'s own historical default was the string "unknown", which is
        a legitimate answer and must still pass. A set that happens to exclude
        "unknown" makes a sloppy normaliser look correct -- that is a property
        of the set, not of the code.
        """
        c = _client_returning(
            _wrap(json.dumps({"category": "unknown", "confidence": 0.1, "reasoning": "r"}))
        )
        assert c.classify("p", "m")[0] == "unknown"

    def test_a_null_reasoning_is_coerced_not_raised(self):
        """reasoning is free text feeding a trace line, not a decision.

        `data.get("reasoning", "")` returns None when the key is PRESENT and
        null -- the default only fires on absence. Coercing is right here
        precisely because nothing branches on it; the same coercion on
        `category` would be the forbidden one.
        """
        c = _client_returning(
            _wrap(json.dumps({"category": "Newsletter", "confidence": 0.9, "reasoning": None}))
        )
        assert c.classify("p", "m")[2] == ""


class TestTheApiKeyReachesTheRequest:
    """Asserted on the outgoing request, not on the constructor: a header set
    in __init__ and dropped at a call site looks identical from __init__.
    """

    @staticmethod
    @contextmanager
    def _mock_transport(client: LLMClient, handler) -> Iterator[None]:
        """Route the client's requests through `handler`.

        Built from the REAL client's headers, so a constructor that stops
        setting them leaves the handler seeing nothing.
        """
        original = client.client
        client.client = httpx.Client(
            transport=httpx.MockTransport(handler), headers=original.headers
        )
        try:
            yield
        finally:
            client.client.close()
            original.close()

    @classmethod
    def _authorization_seen(
        cls, client: LLMClient, path: str = "/v1/chat/completions"
    ) -> str | None:
        seen: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen[request.url.path] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}]},
            )

        with cls._mock_transport(client, handler):
            client._post_completion(model="m", prompt="p", max_tokens=10)
        return seen.get(path)

    def test_base_url_is_normalised(self):
        """A trailing slash is stripped. Asserted directly, so the blank-key
        warning is not its only guard.
        """
        assert LLMClient("http://llm.test/", api_key="k").base_url == "http://llm.test"

    def test_a_key_is_sent_as_a_bearer_header(self):
        c = LLMClient("http://llm.test", api_key="sk-secret")
        assert self._authorization_seen(c) == "Bearer sk-secret"

    def test_no_key_sends_no_authorization_header(self):
        c = LLMClient("http://llm.test")
        assert self._authorization_seen(c) is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
    def test_a_blank_key_sends_no_authorization_header(self, blank):
        c = LLMClient("http://llm.test", api_key=blank)
        assert self._authorization_seen(c) is None

    @pytest.mark.parametrize("padded", ["  sk-secret", "sk-secret\n", " sk-secret \t"])
    def test_surrounding_whitespace_is_stripped(self, padded):
        c = LLMClient("http://llm.test", api_key=padded)
        assert self._authorization_seen(c) == "Bearer sk-secret"

    @pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
    # The slashed URL is the second case because the message must carry the
    # NORMALISED base_url. A positive substring check cannot tell the two
    # apart, the normalised URL being a prefix of the slashed one, so the
    # negative assertion below is what catches a raw `base_url`.
    @pytest.mark.parametrize("url", ["http://llm.test", "http://llm.test/"])
    def test_a_blank_key_is_reported(self, blank, url, capsys):
        """Including "", which an env var set to nothing produces."""
        LLMClient(url, api_key=blank)
        out = capsys.readouterr().out
        assert "api_key is blank; sending no Authorization header to http://llm.test" in out
        assert "header to http://llm.test/" not in out
        # In repr form only. A mutant appending the raw value survives, and
        # only blank values reach this branch, so that is left alone.
        assert repr(blank) not in out

    @pytest.mark.parametrize("fine", [None, "sk-secret"])
    def test_a_usable_or_absent_key_is_silent(self, fine, capsys):
        """No key at all is the normal direct-ollama configuration, and a
        working key is the normal proxy one. Neither gets a warning -- without
        this, a guard that warns on every construction passes every other test
        here.

        Asserted as NO output rather than as the absence of a keyword, so a
        reworded warning cannot pass this test quietly.
        """
        LLMClient("http://llm.test", api_key=fine)
        captured = capsys.readouterr()
        assert captured.out + captured.err == ""

    def test_a_real_key_is_never_logged(self, capsys):
        LLMClient("http://llm.test", api_key="sk-secret")
        captured = capsys.readouterr()
        assert "sk-secret" not in captured.out + captured.err

    def test_the_native_fallback_carries_the_key_too(self):
        """The /api/generate retry is a SECOND request, and a header added only
        to the chat-completions call would leave it unauthenticated.
        """
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.headers.get("authorization")))
            if request.url.path.endswith("/api/generate"):
                return httpx.Response(200, json={"response": '{"ok": 1}', "done_reason": "stop"})
            # Empty content with a normal stop: unreadable, so the client
            # falls back to the native endpoint.
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            )

        c = LLMClient("http://llm.test", api_key="sk-secret")
        with self._mock_transport(c, handler):
            c._post_completion(model="m", prompt="p", max_tokens=10, reject_truncated=False)

        paths = [p for p, _ in seen]
        assert any(p.endswith("/api/generate") for p in paths), paths
        assert {a for _, a in seen} == {"Bearer sk-secret"}


class TestAnHttpFaultIsAnLLMError:
    """An HTTP fault escaped as httpx.HTTPStatusError, which is not an
    LLMError -- so a caller branching on LLMError never saw it (utils-3258).
    """

    # A body that PARSES, so the status check is the only thing that can
    # raise. With an unparseable one these tests pass without the fix.
    GOOD_BODY = {"choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}]}

    @classmethod
    def _client_answering(cls, status: int, path: str = "/v1/chat/completions", body=None):
        c = LLMClient("http://llm.test", api_key="k")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == path:
                if body is not None:
                    return httpx.Response(status, content=body)
                return httpx.Response(status, json=cls.GOOD_BODY)
            # The primary call succeeds with EMPTY content, which is what
            # sends _post_completion to the native endpoint.
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            )

        original = c.client
        c.client = httpx.Client(transport=httpx.MockTransport(handler), headers=original.headers)
        original.close()
        return c

    @staticmethod
    def _post(c: LLMClient):
        return c._post_completion(model="m", prompt="p", max_tokens=10)

    def test_a_401_raises_an_llm_error_naming_the_status(self):
        """The cascade's requirement (an LLMError) and the diagnosis (which
        status) in one assertion, over a body that would otherwise parse.
        """
        c = self._client_answering(401)
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert isinstance(caught.value, LLMHTTPError)
        assert caught.value.status_code == 401
        assert "401" in str(caught.value)

    def test_the_message_names_which_request_failed(self):
        """`where` is the only thing distinguishing the two POSTs in a log."""
        c = self._client_answering(401)
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "chat completions" in str(caught.value)

    def test_a_401_is_not_retryable(self):
        c = self._client_answering(401)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.retryable is False

    @pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503])
    def test_a_transient_fault_stays_retryable(self, status):
        c = self._client_answering(status)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.retryable is True

    @pytest.mark.parametrize("status", [400, 403, 404, 413, 422, 431, 501, 505])
    def test_a_permanent_fault_is_not_retryable(self, status):
        """413, 431, 501 and 505 are the ones a status list tends to miss."""
        c = self._client_answering(status)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.retryable is False

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_a_redirect_is_an_llm_error_too(self, status):
        """`is_error` is 4xx/5xx only, so a 3xx needs the is_success guard to
        become an LLMError rather than a raw JSONDecodeError. Permanent: this
        client does not follow redirects.
        """
        c = self._client_answering(status)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.status_code == status
        assert caught.value.retryable is False

    def test_a_200_that_is_not_json_is_an_llm_error(self):
        """A proxy answering 200 with an HTML page. .json() raises
        ValueError, which is not an LLMError and so bypasses a cascade.
        """
        c = self._client_answering(200, body=b"<html>no</html>")
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "not JSON" in str(caught.value)
        assert "chat completions" in str(caught.value), "which request failed"

    def test_the_fault_chains_the_original(self):
        """`from e` -- the httpx or ValueError cause stays reachable for a
        traceback, which is the only place the proxy's own body survives.
        """
        c = self._client_answering(200, body=b"<html>no</html>")
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.__cause__ is not None

    def test_llm_http_error_is_exported(self):
        """A caller cannot catch what the package does not export."""
        import cortex_utils.llm as pkg

        assert pkg.LLMHTTPError is LLMHTTPError
        assert "LLMHTTPError" in pkg.__all__

    def test_the_native_fallback_404_reports_the_empty_response(self):
        """/api/generate is an OLLAMA route; the proxy answers 404. The
        fallback's own failure must not become the caller's diagnosis -- the
        empty response is the finding, and a cascading caller needs an
        LLMError so it tries its next tier.
        """
        c = self._client_answering(404, path="/api/generate")
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "empty response from chat completions" in str(caught.value)
        assert "404" in str(caught.value), "the status, not just the type name"
        assert caught.value.retryable is False, "a 404 route is permanent"
        # Chained, so the fallback's own fault survives in a traceback even
        # though the message is about the empty response.
        assert isinstance(caught.value.__cause__, LLMHTTPError)
        assert caught.value.__cause__.status_code == 404

    def test_a_dead_native_host_does_not_leak_httpx(self):
        """A CONNECTION failure on the fallback, not an HTTP status. Narrowing
        the except tuple to LLMError alone survived every other test here, and
        a dead /api/generate host would then leak a raw httpx exception past a
        cascade -- the defect this PR exists to fix, on the path it adds.
        """
        c = LLMClient("http://llm.test", api_key="k")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/generate"):
                raise httpx.ConnectError("no route to host")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            )

        original = c.client
        c.client = httpx.Client(transport=httpx.MockTransport(handler), headers=original.headers)
        original.close()
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "empty response from chat completions" in str(caught.value)
        # A CONNECTION failure is retryable, and httpx.RequestError has no
        # `retryable`, so this is what the getattr default decides.
        assert caught.value.retryable is True

    def test_a_non_object_body_on_the_primary_path_is_an_llm_error(self):
        """`null`, `[1]` and `"x"` are valid JSON. The object check was on the
        native call site only, so a non-object here reached `result.get` in
        _reject_if_truncated and escaped as a raw AttributeError.
        """
        for body in (b"null", b"[1]", b'"x"'):
            c = self._client_answering(200, body=body)
            try:
                with pytest.raises(LLMError) as caught:
                    self._post(c)
            finally:
                c.client.close()
            assert "not an object" in str(caught.value), body
            assert "chat completions" in str(caught.value), "which request failed"

    def test_a_non_json_body_on_the_native_path_is_an_llm_error(self):
        """The native call used .json() bare, so dropping ValueError from its
        except tuple survived every other test here.
        """
        c = self._client_answering(200, path="/api/generate", body=b"<html>no</html>")
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "empty response from chat completions" in str(caught.value)
        assert "native generate" in str(caught.value), "which request failed"

    @pytest.mark.parametrize("status", [432, 451, 499])
    def test_an_unlisted_4xx_is_not_retryable(self, status):
        """Nothing between 432 and 499 was exercised, so `< 500` could have
        been `< 499` unnoticed.
        """
        c = self._client_answering(status)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert caught.value.retryable is False

    # EVERY re-raising public method, not just one. Each has its own
    # `except LLMError: raise` arm; patched independently, five of the six
    # were unguarded against an HTTP status, and `except Exception` would
    # then rewrap a 401 as a plain LLMError with retryable defaulting True.
    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda c: c.check_intent("subj", "p", "m"), id="check_intent"),
            pytest.param(lambda c: c.classify("p", "m"), id="classify"),
            pytest.param(
                lambda c: c.check_email_intent("a@b.test", "subj", "body", "p", "m"),
                id="check_email_intent",
            ),
            pytest.param(
                lambda c: c.categorize_email("a@b.test", "subj", "body", "p", "m", ["admin"]),
                id="categorize_email",
            ),
            pytest.param(
                lambda c: c.classify_with_extraction("p", "m"),
                id="classify_with_extraction",
            ),
        ],
    )
    def test_a_401_through_a_public_method_keeps_its_status(self, call):
        c = self._client_answering(401)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                call(c)
        finally:
            c.client.close()
        assert caught.value.status_code == 401
        assert caught.value.retryable is False

    def test_the_message_carries_the_reason_phrase(self):
        """The alerter's severity patterns match on the TEXT: without
        "Unauthorized" a 401 classifies as a generic LLM error rather than an
        authentication failure (alerter/classifier.py).
        """
        c = self._client_answering(401)
        try:
            with pytest.raises(LLMHTTPError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "401 Unauthorized" in str(caught.value)

    def test_an_http_fault_is_not_logged_at_error_by_the_library(self, capsys):
        """extract_value treats an HTTP fault as OPTIONAL and logs it at
        warning. A logger.error in _raise_for_status runs upstream of that
        choice, and the alerter matches level=error -- so re-adding one would
        alert on every optional extraction failure.
        """
        c = self._client_answering(401)
        try:
            assert c.extract_value("a@b.test", "subj", "body", "p", "m") is None
        finally:
            c.client.close()
        out = capsys.readouterr()
        logged = out.out + out.err
        # BOTH renderers: structlog emits JSON once configure_logging has run
        # (as the services do) and the console format otherwise (as the suite
        # does). Asserting only the JSON form made this test vacuous -- a
        # re-added logger.error survived it.
        assert '"level": "error"' not in logged
        assert "[error" not in logged

    def test_extract_value_still_swallows_an_http_fault(self):
        """It returns None by design -- extraction is optional. The deleted
        handler did that explicitly; the surviving `except LLMError` must do
        the same.
        """
        c = self._client_answering(401)
        try:
            assert c.extract_value("a@b.test", "subj", "body", "p", "m") is None
        finally:
            c.client.close()

    def test_a_native_body_that_is_not_an_object_is_an_llm_error(self):
        """native_result.get would raise AttributeError on a list or a
        string, which is not an LLMError.
        """
        c = LLMClient("http://llm.test", api_key="k")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/generate"):
                return httpx.Response(200, json=["not", "an", "object"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            )

        original = c.client
        c.client = httpx.Client(transport=httpx.MockTransport(handler), headers=original.headers)
        original.close()
        try:
            with pytest.raises(LLMError) as caught:
                self._post(c)
        finally:
            c.client.close()
        assert "not an object" in str(caught.value)
