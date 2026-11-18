from unittest import mock

import auto_result_collect as arc


def test_uses_stored_deadline_without_official_fetch():
    stored = mock.Mock(return_value={"03": {"1": "10:59"}})
    fetch = mock.Mock()
    cache = {}
    assert arc._deadline_hhmm("20261118", "03", 1, cache, stored=stored, fetch=fetch) == "10:59"
    assert arc._deadline_hhmm("20261118", "03", 1, cache, stored=stored, fetch=fetch) == "10:59"
    fetch.assert_not_called()
    assert stored.call_count == 1  # 同じ日は1回だけ読む


def test_falls_back_to_official_once_per_venue():
    stored = mock.Mock(return_value={})
    fetch = mock.Mock(return_value={1: "10:22", 2: "10:50"})
    cache = {}
    assert arc._deadline_hhmm("20261118", "08", 1, cache, stored=stored, fetch=fetch) == "10:22"
    assert arc._deadline_hhmm("20261118", "08", 2, cache, stored=stored, fetch=fetch) == "10:50"
    fetch.assert_called_once_with("20261118", "08")
