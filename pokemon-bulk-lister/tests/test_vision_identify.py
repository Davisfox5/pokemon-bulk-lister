"""lib/vision_identify: which model is asked, and what it is told."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from lib import vision_identify as vi


class FakeClient:
    def __init__(self, refuse=()):
        self.calls = []
        self.refuse = refuse
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] in self.refuse:
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError("unsupported", response=httpx2.Response(400, request=request), body=None)
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=json.dumps({"name": "Pikachu"}))])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("VISION_MODEL", raising=False)
    fake = FakeClient()
    monkeypatch.setattr(vi, "_client", lambda: fake)
    from lib import card_catalog
    monkeypatch.setattr(card_catalog, "prompt_reference", lambda n=60: "30th Celebration | id me55 | printed code 30C | 128 cards | 2026/09/16")
    return fake


def test_the_current_haiku_reads_the_card_with_the_newest_sets_in_hand(client):
    assert vi._vision_request("aGk=", "Identify this card.", vi._CARD_SCHEMA, 1024, None) == {"name": "Pikachu"}
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "low" and call["max_tokens"] == 2048     # room for its thinking
    assert "temperature" not in call and "thinking" not in call
    assert "30th Celebration | id me55 | printed code 30C" in call["system"]
    assert "never fill them in from memory" in call["system"]
    props = call["output_config"]["format"]["schema"]["properties"]
    assert "number_total" in props and "set_code_printed" in props


def test_a_refused_request_is_asked_again_of_the_previous_haiku(client):
    client.refuse = ("claude-haiku-5-5",)
    assert vi._vision_request("aGk=", "Identify this card.", vi._CARD_SCHEMA, 1024, None) == {"name": "Pikachu"}
    assert [c["model"] for c in client.calls] == ["claude-haiku-5-5", "claude-haiku-4-5"]
    again = client.calls[1]
    assert "effort" not in again["output_config"] and again["max_tokens"] == 1024


def test_the_set_list_is_left_out_when_the_catalog_has_none(client, monkeypatch):
    from lib import card_catalog
    monkeypatch.setattr(card_catalog, "prompt_reference", lambda n=60: "")
    vi._vision_request("aGk=", "Identify this card.", vi._CARD_SCHEMA, 1024, None)
    assert "Recent sets" not in client.calls[0]["system"]
