"""LLM_REASONING_EFFORT is opt-in: unset sends nothing (utils-a4d3 sibling, triage-7c5a)."""

from __future__ import annotations

from unittest import mock

import httpx
import pytest

from cortex_utils.llm.client import LLMClient


def _posts(monkeypatch: pytest.MonkeyPatch, value: str | None, content: str = "yes") -> list[dict]:
    if value is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", value)
    c = LLMClient("http://llm.test")
    sent: list[dict] = []

    def post(url: str, json: dict | None = None, **kw: object) -> httpx.Response:
        sent.append({"url": url, **(json or {})})
        if url.endswith("/api/generate"):
            body: dict = {"response": "yes", "done_reason": "stop"}
        else:
            body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    c.client = mock.Mock()
    c.client.post.side_effect = post
    c.check_intent("subject", "Is this {subject}? yes or no", "gemma4:26b")
    return sent


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_sends_no_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    [chat] = _posts(monkeypatch, value)
    assert "reasoning_effort" not in chat


@pytest.mark.parametrize("value", ["none", " none "])
def test_set_is_sent_stripped_on_the_chat_call(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    [chat] = _posts(monkeypatch, value)
    assert chat["reasoning_effort"] == "none"


def test_the_generate_fallback_does_not_carry_it(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, native = _posts(monkeypatch, "none", content="")
    assert chat["url"].endswith("/v1/chat/completions")
    assert native["url"].endswith("/api/generate")
    assert "reasoning_effort" not in native
