from unittest import mock

import track_odds as t


def _stored(_hd):
    return {"03": {"1": "10:59", "4": "12:17"}}


def test_uses_stored_deadline_without_official_fetch():
    fetch = mock.Mock()
    cache = {}
    with mock.patch.object(t, "_stored_deadlines", side_effect=_stored) as s:
        d1 = t._deadline_for("2026-11-18", "3", 1, cache, fetch=fetch)
        d4 = t._deadline_for("20261118", "03", 4, cache, fetch=fetch)
    assert (d1.hour, d1.minute) == (10, 59)
    assert (d4.hour, d4.minute) == (12, 17)
    assert d1.tzinfo is t.JST
    fetch.assert_not_called()
    assert s.call_count == 1  # 同じ日は1回だけ読む


def test_falls_back_to_official_when_not_stored():
    fetch = mock.Mock(return_value="official")
    with mock.patch.object(t, "_stored_deadlines", side_effect=_stored):
        assert t._deadline_for("20261118", "08", 1, {}, fetch=fetch) == "official"
    fetch.assert_called_once_with("20261118", "08", 1)


def test_bad_stored_value_falls_back():
    fetch = mock.Mock(return_value=None)
    with mock.patch.object(t, "_stored_deadlines", return_value={"03": {"1": ""}}):
        assert t._deadline_for("20261118", "03", 1, {}, fetch=fetch) is None
    fetch.assert_called_once()
