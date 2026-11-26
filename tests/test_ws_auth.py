"""WebSocket token and Origin rejection."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glance.server.app import (
    SUBPROTOCOL,
    Hub,
    create_app,
    extract_token,
    origin_allowed,
)

PORT = 8765
GOOD_ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = "s3ssi0n-t0ken"


def make_client():
    app = create_app(
        hub=Hub(),
        inbox=asyncio.Queue(),
        token=TOKEN,
        allowed_origins={GOOD_ORIGIN, f"http://localhost:{PORT}"},
        mode_names=["summarize"],
    )
    return TestClient(app)


def connect(client, *, token=TOKEN, origin=GOOD_ORIGIN, subprotocols=None):
    if subprotocols is None:
        subprotocols = [SUBPROTOCOL] + ([f"token.{token}"] if token is not None else [])
    headers = {"origin": origin} if origin is not None else {}
    return client.websocket_connect("/ws", subprotocols=subprotocols, headers=headers)


# -- units -------------------------------------------------------------------

def test_extract_token_finds_the_token_subprotocol():
    assert extract_token([SUBPROTOCOL, "token.abc"]) == "abc"
    assert extract_token([SUBPROTOCOL]) is None
    assert extract_token(None) is None


def test_origin_allowed_fails_closed_on_a_missing_origin():
    allowed = {GOOD_ORIGIN}
    assert origin_allowed(GOOD_ORIGIN, allowed)
    assert not origin_allowed(None, allowed)          # non-browser client
    assert not origin_allowed("", allowed)
    assert not origin_allowed("null", allowed)        # sandboxed iframe
    assert not origin_allowed("http://evil.example", allowed)
    assert not origin_allowed(f"http://127.0.0.1:{PORT + 1}", allowed)  # wrong port


# -- integration -------------------------------------------------------------

def test_the_right_token_is_accepted():
    with make_client() as client, connect(client) as ws:
        assert ws.accepted_subprotocol == SUBPROTOCOL


def test_a_wrong_token_is_rejected():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, token="wrong-token"):
                pass
        assert e.value.code == 1008


def test_a_missing_token_is_rejected():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, token=None):
                pass
        assert e.value.code == 1008


def test_an_empty_token_is_rejected():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, subprotocols=[SUBPROTOCOL, "token."]):
                pass
        assert e.value.code == 1008


def test_a_token_prefix_is_not_enough():
    """compare_digest, not startswith."""
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, token=TOKEN[:-1]):
                pass
        assert e.value.code == 1008


def test_a_foreign_origin_is_rejected_even_with_the_right_token():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, origin="http://evil.example"):
                pass
        assert e.value.code == 1008


def test_a_missing_origin_is_rejected_even_with_the_right_token():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as e:
            with connect(client, origin=None):
                pass
        assert e.value.code == 1008


def test_followups_from_the_page_reach_the_inbox():
    inbox: asyncio.Queue = asyncio.Queue()
    app = create_app(
        hub=Hub(), inbox=inbox, token=TOKEN,
        allowed_origins={GOOD_ORIGIN}, mode_names=["summarize"],
    )
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws", subprotocols=[SUBPROTOCOL, f"token.{TOKEN}"],
            headers={"origin": GOOD_ORIGIN},
        ) as ws:
            ws.send_json({"type": "followup", "text": "what is the error?"})
            for _ in range(50):
                if not inbox.empty():
                    break
                import time
                time.sleep(0.02)
    ev = inbox.get_nowait()
    assert ev.kind == "followup"
    assert ev.text == "what is the error?"


def test_security_headers_are_present():
    with make_client() as client:
        r = client.get("/")
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "object-src 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
