"""LLM_REASONING_EFFORT is opt-in: unset sends nothing (utils-a4d3 sibling, triage-7c5a)."""

from __future__ import annotations

from unittest import mock

import httpx
import pytest

from cortex_utils.llm.client import LLMClient


def _chat_payload(monkeypatch: pytest.MonkeyPatch, value: str | None) -> dict:
    if value is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", value)
    c = LLMClient("http://llm.test")
    sent: list[dict] = []

    def post(url: str, json: dict | None = None, **kw: object) -> httpx.Response:
        sent.append(json or {})
        body = {"choices": [{"message": {"content": "yes"}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    c.client = mock.Mock()
    c.client.post.side_effect = post
    c.check_intent("subject", "Is this {subject}? yes or no", "gemma4:26b")
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_sends_no_reasoning_effort(monkeypatch, value) -> None:
    assert "reasoning_effort" not in _chat_payload(monkeypatch, value)


def test_set_is_sent_on_the_chat_call(monkeypatch) -> None:
    assert _chat_payload(monkeypatch, "none")["reasoning_effort"] == "none"
