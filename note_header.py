"""
note記事の見出し画像（管理者だけ）。

画像は assets/note_headers/ に置く（公開リポジトリ。アプリと一緒に配られるので、ネットワーク無しで読める）。
記事ごとに次の順で1枚を選び、note記事の欄に出す（自分で別の画像に変えられる）。

  1. 万舟チャンスに選ばれたレース                 → 万舟チャンス
  2. 中穴〜万舟狙いの買い目を記事に載せる時       → 高配当を狙う
  3. 風速 WIND_MIN m/s 以上か波高 WAVE_MIN cm 以上 → 荒れ水面を攻略
  4. 展示前厳選（当たりやすいレース）のレース     → 本日の厳選レース
  5. どれにも当てはまらない時                     → 本日の勝負レース

荒れ水面の基準: 2025-01〜2026-10 の約10万レースで、風速6m/s以上か波高6cm以上は全体の約8.5%
（約12レースに1つ）。その万舟率は約18.2%（全体 16.5%）。5m/s・3cm（予想の補正の基準）だと全体の43%が
当てはまり「荒れ水面」と呼ぶには多すぎるため、6m/s・6cm にした。

使った画像は note_publications.header_image（と自動で選んだ画像・理由）に記事ごとに残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "note_headers"

WIND_MIN = 6.0   # m/s
WAVE_MIN = 6.0   # cm


@dataclass(frozen=True)
class Header:
    key: str
    label: str
    file: str

    @property
    def path(self):
        return ASSET_DIR / self.file

    def exists(self):
        return self.path.exists()


HEADERS = {
    "manshu": Header("manshu", "万舟チャンス", "manshu.jpg"),
    "kohaito": Header("kohaito", "高配当を狙う", "kohaito.jpg"),
    "aresuimen": Header("aresuimen", "荒れ水面を攻略", "aresuimen.jpg"),
    "gensen": Header("gensen", "本日の厳選レース", "gensen.jpg"),
    "shobu": Header("shobu", "本日の勝負レース", "shobu.jpg"),
}
DEFAULT = "shobu"


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def water(work):
    """直前情報の風速（m/s）・波高（cm）。work は予想の表（wind_speed・wave_height か wave_cm の列）か dict。
    艇ごとに同じ値が入っているので、読めた値の最大を使う。読めなければ None。"""
    if work is None:
        return None, None

    def pick(cols):
        vals = []
        for c in cols:
            if hasattr(work, "columns"):
                if c in work.columns:
                    vals += [_num(x) for x in work[c].tolist()]
            elif isinstance(work, dict) and c in work:
                vals.append(_num(work[c]))
        vals = [x for x in vals if x is not None]
        return max(vals) if vals else None

    return pick(("wind_speed",)), pick(("wave_height", "wave_cm"))


def is_rough(wind, wave):
    return (wind is not None and wind >= WIND_MIN) or (wave is not None and wave >= WAVE_MIN)


def choose(manshu_chance=False, with_aim=False, wind=None, wave=None, pre_pick=False):
    """上から順に当てはまる画像の (key, 理由)。その画像のファイルがまだ無ければ、次に当てはまるものにする。"""
    rules = [
        (manshu_chance, "manshu", "万舟チャンスに選ばれたレース"),
        (with_aim, "kohaito", "中穴〜万舟狙いの買い目を記事に載せる"),
        (is_rough(wind, wave), "aresuimen", water_text(wind, wave)),
        (pre_pick, "gensen", "展示前厳選（当たりやすいレース）のレース"),
    ]
    skipped = []
    for hit, key, why in rules:
        if not hit:
            continue
        if HEADERS[key].exists():
            return key, why + (f"（{'・'.join(skipped)}）" if skipped else "")
        skipped.append(f"「{HEADERS[key].label}」の画像がまだ無いため次の候補")
    return DEFAULT, "どれにも当てはまらない" + (f"（{'・'.join(skipped)}）" if skipped else "")


def water_text(wind, wave):
    parts = []
    if wind is not None:
        parts.append(f"風速{wind:g}m/s")
    if wave is not None:
        parts.append(f"波高{wave:g}cm")
    return f"荒れ水面（{'・'.join(parts) or '風・波'}。基準: 風速{WIND_MIN:g}m/s以上か波高{WAVE_MIN:g}cm以上）"


def available():
    """ファイルのある画像の key（選ぶ欄に並べる順）。"""
    return [k for k, h in HEADERS.items() if h.exists()]


def with_aim_in_article(text):
    """記事の本文に中穴〜万舟狙いの買い目が載っているか（見出しか「万舟狙い」の言葉で見る）。"""
    import manshu

    t = str(text or "")
    return manshu.AIM_LABEL in t or "万舟狙い" in t


def file_name(key, date_iso, label):
    return f"{date_iso}_{label}_{HEADERS[key].label}.jpg".replace(" ", "")
