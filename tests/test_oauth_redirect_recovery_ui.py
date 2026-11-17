"""Real-browser synthetic OAuth recovery; no external client/account traffic."""
import base64
import hashlib
import os
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
from tests.browser_launch import launch_browser


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_browser_recovers_then_requires_login_and_consent(stack, engine):
    callback = "https://antigravity.google/oauth-callback"
    broken = "h ttps://antigravity.google/oauth-callback"
    registration = stack.must(stack.client.post("/oauth/register", json={
        "client_name": "Synthetic Antigravity recovery", "redirect_uris": [callback]}))
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": registration["client_id"], "redirect_uri": broken,
              "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": challenge, "resource": stack.url + "/mcp",
              "scope": "read", "state": "browser+state&=中文"}
    with sync_playwright() as pw:
        override = os.getenv("CODEPIER_TEST_CHROMIUM_EXECUTABLE") if engine == "chromium" else None
        browser = launch_browser(getattr(pw, engine), **({"executable_path": override} if override else {}))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        # Only the callback is synthetic; Hub, login, consent and code/token
        # lifecycle run against the real isolated HTTP stack.
        page.route(callback + "*", lambda route: route.fulfill(body="Synthetic callback"))
        responses = []
        page.on("response", lambda r: responses.append(r) if "/oauth/authorize?" in r.url else None)
        page.goto(stack.url + "/oauth/authorize?" + urlencode(params))
        expect(page.locator("#login-form")).to_be_visible()
        assert len(responses) == 2
        assert all(r.status == 303 for r in responses)
        assert urlsplit(responses[0].headers["location"]).path == "/oauth/authorize"
        corrected = parse_qs(urlsplit(responses[0].headers["location"]).query)
        assert corrected["redirect_uri"] == [callback]
        assert corrected["state"] == [params["state"]]
        assert page.url.startswith(stack.url + "/?authorize=")
        page.fill("#username", "admin")
        page.fill("#password", stack.password)
        page.click("#login-form button")
        expect(page.locator("#allow-read")).to_be_visible()
        assert not page.url.startswith(callback)
        page.click("#allow-read")
        page.wait_for_url(callback + "?**")
        query = parse_qs(urlsplit(page.url).query)
        assert query["state"] == [params["state"]]
        form = {"grant_type": "authorization_code", "client_id": registration["client_id"],
                "code": query["code"][0], "redirect_uri": callback, "code_verifier": verifier}
        assert stack.client.post("/oauth/token", data={**form, "redirect_uri": broken}).status_code == 400
        tokens = stack.must(stack.client.post("/oauth/token", data=form))
        assert tokens["access_token"]
        assert stack.client.post("/oauth/token", data=form).status_code == 400
        assert not errors, errors
        browser.close()
