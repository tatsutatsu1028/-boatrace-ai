"""Threadsトークン登録（長期トークン交換のフォールバック）のテスト。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import threads_poster as tp  # noqa: E402


class _Resp:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _fake_requests(monkeypatch, exchange_status=200, me_ok=True, refresh_ok=True):
    calls = {"saved": [], "urls": []}

    def get(url, params=None, headers=None, timeout=None):
        calls["urls"].append(url)
        if url == tp.EXCHANGE_URL:
            if exchange_status >= 400:
                return _Resp(exchange_status, text="already long-lived")
            return _Resp(200, {"access_token": "LONG"})
        if url == tp.REFRESH_URL:
            return _Resp(200, {"access_token": "REFRESHED"}) if refresh_ok else _Resp(400)
        if url.endswith("/me"):
            if me_ok:
                return _Resp(200, {"id": "123", "username": "boat"})
            return _Resp(400, text="invalid token")
        if "/rest/v1/" in url:
            return _Resp(200, [{"user_id": "123", "access_token": "OLD", "token_updated_at": None}])
        raise AssertionError(url)

    def post(url, headers=None, json=None, timeout=None):
        calls["saved"].append(json)
        return _Resp(201)

    monkeypatch.setattr(tp.requests, "get", get)
    monkeypatch.setattr(tp.requests, "post", post)
    monkeypatch.setenv("THREADS_APP_SECRET", "secret")
    return calls


def test_exchange_success_saves_long_token(monkeypatch):
    calls = _fake_requests(monkeypatch)
    res = tp.save_config("https://sb", "key", "", "SHORT")
    assert res == {"exchanged": True, "exchange_error": None}
    assert calls["saved"][0]["access_token"] == "LONG"
    assert calls["saved"][0]["user_id"] == "123"


def test_exchange_error_saves_token_as_is(monkeypatch):
    calls = _fake_requests(monkeypatch, exchange_status=400)
    res = tp.save_config("https://sb", "key", "123", "ALREADY_LONG")
    assert res["exchanged"] is False and res["exchange_error"]
    assert calls["saved"][0]["access_token"] == "ALREADY_LONG"


def test_missing_secret_saves_token_as_is(monkeypatch):
    calls = _fake_requests(monkeypatch)
    monkeypatch.delenv("THREADS_APP_SECRET")
    res = tp.save_config("https://sb", "key", "", "ALREADY_LONG")
    assert res["exchanged"] is False
    assert calls["saved"][0]["access_token"] == "ALREADY_LONG"


def test_invalid_token_is_not_saved(monkeypatch):
    calls = _fake_requests(monkeypatch, exchange_status=400, me_ok=False)
    with pytest.raises(RuntimeError, match="無効"):
        tp.save_config("https://sb", "key", "123", "BAD")
    assert calls["saved"] == []


def test_refresh_does_not_exchange(monkeypatch):
    calls = _fake_requests(monkeypatch)
    assert tp.refresh_access_token("https://sb", "key") == "REFRESHED"
    assert tp.EXCHANGE_URL not in calls["urls"]
    assert calls["saved"][0]["access_token"] == "REFRESHED"
    assert calls["saved"][0]["token_updated_at"]
