"""Authorization-link diagnostics preserve exact redirect matching."""
import pytest
from tests.test_audit_api import api as api
from tests.test_oauth_antigravity import antigravity_api as antigravity_api, register, CALLBACK


@pytest.mark.parametrize("redirect, hint", [
    ("", "缺少回调地址"),
    ("h ttps://antigravity.google/oauth-callback", "含空格"),
    (" " + CALLBACK, "含空格"),
    (CALLBACK + "\t", "含空格"),
    (CALLBACK + "\n", "含空格"),
    (CALLBACK + "\x00", "含空格"),
    (CALLBACK + "\x7f", "含空格"),
    (CALLBACK + "\u00a0", "含空格"),
    (CALLBACK + "\\suffix", "含空格"),
    (CALLBACK + "/", "登记地址不一致"),
    (CALLBACK + "?unexpected=1", "登记地址不一致"),
    (CALLBACK.replace("https", "http"), "登记地址不一致"),
    (CALLBACK.replace("antigravity.google", "ANTIGRAVITY.GOOGLE"), "登记地址不一致"),
    (CALLBACK + "#fragment", "登记地址不一致"),
])
def test_broken_authorization_link_is_rejected_without_reflection(antigravity_api, redirect, hint):
    app, client, _ = antigravity_api
    client_id = register(client).json()["client_id"]
    params = {"client_id": client_id, "redirect_uri": redirect,
              "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": "a" * 43, "state": "private-state-marker"}
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REDIRECT"
    assert hint in response.json()["error"]["message"]
    assert "重新连接" in response.json()["error"]["message"]
    assert "location" not in response.headers
    assert client_id not in response.text
    assert "private-state-marker" not in response.text
    assert CALLBACK not in response.text
    # A failed request must not consume or corrupt the registered client.
    response = client.get("/oauth/authorize", params={**params, "redirect_uri": CALLBACK},
                          follow_redirects=False)
    assert response.status_code == 303


def test_missing_redirect_parameter_is_actionable(antigravity_api):
    _, client, _ = antigravity_api
    response = client.get("/oauth/authorize",
                          params={"client_id": register(client).json()["client_id"]},
                          follow_redirects=False)
    assert response.status_code == 400
    assert "缺少回调地址" in response.json()["error"]["message"]
