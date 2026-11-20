"""lib/anthropic_auth: a key when there is one, the host's identity when not."""
import anthropic
import pytest

from lib import anthropic_auth

_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
         "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_IDENTITY_TOKEN",
         "ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_PROFILE", "VERCEL_OIDC_TOKEN")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(anthropic_auth, "_client", None)
    monkeypatch.setattr(anthropic_auth, "_identity_token", None)


def test_nothing_configured():
    assert not anthropic_auth.configured() and not anthropic_auth.federated()


def test_a_key_is_used_as_before(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_x")      # a key wins over keyless sign-in
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "00000000-0000-0000-0000-000000000000")
    assert anthropic_auth.configured() and not anthropic_auth.federated()
    assert anthropic_auth.client().api_key == "sk-ant-test"


def test_keyless_sign_in_uses_the_hosts_identity_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_abc")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_abc")
    assert anthropic_auth.configured() and anthropic_auth.federated()

    made = {}
    from anthropic.lib import credentials

    class Exchange:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(credentials, "WorkloadIdentityCredentials", Exchange)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: ("client", kw))

    first = anthropic_auth.client()
    assert first[0] == "client" and isinstance(first[1]["credentials"], Exchange)
    assert (made["federation_rule_id"], made["organization_id"], made["service_account_id"], made["workspace_id"]) == (
        "fdrl_abc", "11111111-2222-3333-4444-555555555555", "svac_abc", None)
    assert anthropic_auth.client() is first                 # one client, so one cached access token

    # The identity token is read when an exchange happens, so a rotated one is picked up.
    with pytest.raises(RuntimeError, match="no identity token"):
        made["identity_token_provider"]()
    anthropic_auth.remember_identity_token("jwt-1")
    assert made["identity_token_provider"]() == "jwt-1"
    anthropic_auth.remember_identity_token(None)            # a request without the header changes nothing
    anthropic_auth.remember_identity_token("jwt-2")
    assert made["identity_token_provider"]() == "jwt-2"


def test_a_host_that_gives_the_sdk_its_own_token_is_left_to_the_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_abc")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "/var/run/token")
    assert anthropic_auth.configured() and not anthropic_auth.federated()
