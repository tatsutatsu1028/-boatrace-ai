"""
note記事の見出し画像（note_header.py・note_view.render_header）。Supabase が無くても確かめられる部分。

  python -m pytest tests/test_note_header.py
"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import note_header as nh  # noqa: E402


def _all_images(tmp_path, monkeypatch, skip=()):
    for h in nh.HEADERS.values():
        if h.key in skip:
            continue
        src = nh.ASSET_DIR / h.file
        dst = tmp_path / h.file
        if src.exists():
            shutil.copy(src, dst)
        else:
            dst.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(nh, "ASSET_DIR", tmp_path)


def test_repo_has_four_images():
    assert set(nh.available()) >= {"kohaito", "aresuimen", "gensen", "shobu"}


def test_choose_priority(tmp_path, monkeypatch):
    _all_images(tmp_path, monkeypatch)
    assert nh.choose(manshu_chance=True, with_aim=True, wind=9, pre_pick=True)[0] == "manshu"
    assert nh.choose(with_aim=True, wind=9, pre_pick=True)[0] == "kohaito"
    assert nh.choose(wind=6, pre_pick=True)[0] == "aresuimen"
    assert nh.choose(wave=6, pre_pick=True)[0] == "aresuimen"
    assert nh.choose(wind=5.9, wave=5, pre_pick=True)[0] == "gensen"
    key, why = nh.choose()
    assert key == "shobu" and "当てはまらない" in why


def test_choose_skips_missing_image(tmp_path, monkeypatch):
    _all_images(tmp_path, monkeypatch, skip=("manshu",))
    key, why = nh.choose(manshu_chance=True, pre_pick=True)
    assert key == "gensen" and "万舟チャンス" in why
    assert "manshu" not in nh.available()


def test_water_from_work_table():
    work = pd.DataFrame({"lane": [1, 2], "wind_speed": [6.0, 6.0], "wave_height": [3.0, None]})
    assert nh.water(work) == (6.0, 3.0)
    assert nh.water(pd.DataFrame({"wind_speed": [None]})) == (None, None)
    assert nh.water(None) == (None, None)
    assert nh.is_rough(6, None) and nh.is_rough(None, 6) and not nh.is_rough(5.9, 5.9)
    assert "風速6m/s" in nh.water_text(6.0, 3.0)


def test_with_aim_in_article():
    assert nh.with_aim_in_article("■中穴〜万舟狙いの買い目\n2-1-3")
    assert not nh.with_aim_in_article("■本線\n1-2-3")


def _header_app():
    import datetime as dt

    import pandas as pd
    import streamlit as st

    import note_view

    calls = st.session_state.setdefault("rpc", [])
    note_view._race_flags = lambda ctx, d: (False, st.session_state.get("pre_pick", False))
    note_view._rpc = lambda name, payload: calls.append((name, payload)) or True
    pub = st.session_state.get("pub")
    work = pd.DataFrame({"lane": [1], "wind_speed": [st.session_state.get("wind", 2.0)], "wave_height": [1.0]})
    note_view.render_header("20261020_01_1", dt.date(2026, 10, 20), "2026-10-20", "桐生 1R", pub, work, "本文")


def test_render_header_shows_saves_and_records():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_header_app)
    at.session_state["pre_pick"] = True
    at.run()
    assert not at.exception
    assert at.selectbox[0].value == "gensen"
    assert any("本日の厳選レース" in c.value for c in at.caption)
    assert at.session_state["rpc"] == []  # 確定前は記録しない

    at = AppTest.from_function(_header_app)
    at.session_state["wind"] = 7.0
    at.session_state["pub"] = {"race_key": "20261020_01_1"}
    at.run()
    assert at.selectbox[0].value == "aresuimen"
    name, payload = at.session_state["rpc"][-1]
    assert name == "set_note_header_image" and payload["p_image"] == "aresuimen" and payload["p_auto"] == "aresuimen"
    # 自分で変えたら、その画像を記録する（自動で選んだ画像も残す）
    at.selectbox[0].set_value("shobu").run()
    name, payload = at.session_state["rpc"][-1]
    assert payload["p_image"] == "shobu" and payload["p_auto"] == "aresuimen"

    # 記録済みの画像は、次に開いたときもその画像を出す
    at = AppTest.from_function(_header_app)
    at.session_state["pub"] = {"race_key": "20261020_01_1", "header_image": "kohaito", "header_image_auto": "shobu"}
    at.run()
    assert at.selectbox[0].value == "kohaito" and at.session_state["rpc"] == []
