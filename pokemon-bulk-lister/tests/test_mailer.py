"""lib/mailer: which way a message goes out."""
import pytest

from lib import mailer


class _Reply:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload


def test_resend_is_used_when_its_key_is_set(monkeypatch):
    import requests

    sent = []
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert mailer.configured() is False and mailer.send("a@example.com", "Hi", "Body") == "console"

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("MAIL_FROM", "Cards <no-reply@example.com>")
    monkeypatch.setattr(requests, "post", lambda url, **kw: sent.append((url, kw)) or _Reply(200, {"id": "1"}))
    assert mailer.configured() is True
    assert mailer.send("a@example.com", "Hi", "Body") == "smtp"
    url, kw = sent[0]
    assert url == "https://api.resend.com/emails" and kw["headers"]["Authorization"] == "Bearer re_test"
    assert kw["json"] == {"from": "Cards <no-reply@example.com>", "to": ["a@example.com"],
                          "subject": "Hi", "text": "Body"}

    # A refusal (an unverified domain, say) is an error the caller can show.
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Reply(403, {"message": "domain is not verified"}))
    with pytest.raises(RuntimeError, match="domain is not verified"):
        mailer.send("a@example.com", "Hi", "Body")
