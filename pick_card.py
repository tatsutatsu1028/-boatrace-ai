"""
note 記事用の予想カード画像（PNG）。

会場・レース・締切、本線と抑えの買い目、各艇の1着確率のグラフ、スタート〜1マークの展開図を
1枚にまとめる。買い目と1着確率は記事に載せる予想（モデルのみ予想／両方の予想平均）と同じものを
渡す。図形と文字はすべてここで描く（公式のロゴ・他社の画像は使わない）。

日本語の文字には、サーバーに入っている日本語フォント（packages.txt の fonts-ipafont-gothic など）を使う。
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

from race_visuals import LANE_COLORS
from threads_poster import compact_combos, split_main_cover

W, H_MAX = 1080, 1700
MAX_TICKET_LINES = 5
BG = (12, 20, 38)
PANEL = (24, 36, 62)
INK = (236, 241, 250)
SUB = (150, 166, 196)
ACCENT = (96, 165, 250)
WATER = (20, 58, 104)
FONT_PATHS = (
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
)


def _font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def has_japanese_font():
    return any(os.path.exists(p) for p in FONT_PATHS)


def _boat(draw, cx, cy, lane, r=24, font=None):
    fill, ink = LANE_COLORS.get(lane, ((200, 200, 200), (0, 0, 0)))
    draw.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=8, fill=fill, outline=(210, 220, 235), width=2)
    draw.text((cx, cy), str(lane), font=font, fill=ink, anchor="mm")


def _section(draw, y, title, f):
    draw.text((60, y), title, font=f, fill=ACCENT)
    return y + 46


def render_card(venue, race_no, deadline, race_date, tickets, p_first, visual_rows, source_label="",
                favorite=None):
    """カード画像の PNG（bytes）。

    tickets: [{combo, group}]（記事に載せる買い目）、p_first: {艇番: 1着確率}（同じ予想のもの）、
    visual_rows: race_visuals.build_visual_rows の戻り値（想定STと1マークの並び）。
    favorite: (艇番, 選手名)。無ければ1着確率の最上位。
    """
    img = Image.new("RGB", (W, H_MAX), BG)
    d = ImageDraw.Draw(img)
    f_title, f_big, f_mid, f_small, f_boat = _font(60), _font(46), _font(34), _font(26), _font(28)

    # ヘッダー
    d.rounded_rectangle([40, 36, W - 40, 236], radius=28, fill=PANEL)
    d.text((70, 60), f"{race_date}　展示後予想", font=f_small, fill=SUB)
    d.text((70, 98), f"{venue} {race_no}R", font=f_title, fill=INK)
    d.text((W - 70, 120), f"締切 {deadline or '--:--'}", font=f_mid, fill=INK, anchor="ra")
    if favorite is None and p_first:
        lane = max(p_first, key=lambda k: p_first[k])
        names = {int(r["lane"]): r.get("name") for r in (visual_rows or [])}
        favorite = (lane, names.get(lane) or "")
    if favorite:
        d.text((70, 178), f"◎本命 {favorite[0]}号艇 {favorite[1] or ''}".rstrip(), font=f_mid, fill=INK)

    # 買い目（多いときは MAX_TICKET_LINES 行まで出して残りの数を書く）
    main, cover = (compact_combos(x) for x in split_main_cover(tickets))
    lines = max(1, min(MAX_TICKET_LINES, max(len(main), len(cover))))
    y = 262
    d.rounded_rectangle([40, y, W - 40, y + 86 + lines * 54], radius=24, fill=PANEL)
    for col_x, label, combos in ((70, "本線", main), (W // 2 + 10, "抑え", cover)):
        d.text((col_x, y + 22), label, font=f_mid, fill=ACCENT)
        cy = y + 74
        for c in (combos or ["なし"])[:MAX_TICKET_LINES]:
            d.text((col_x, cy), c, font=f_big, fill=INK)
            cy += 54
        if len(combos) > MAX_TICKET_LINES:
            d.text((col_x + 300, cy - 44), f"ほか{len(combos) - MAX_TICKET_LINES}", font=f_small, fill=SUB)
    y += 86 + lines * 54 + 30

    # 1着確率のグラフ
    y = _section(d, y, "各艇の1着確率", f_mid)
    axis = max(0.1, max(p_first.values()) if p_first else 0.1)
    for lane in range(1, 7):
        p = float(p_first.get(lane, 0.0))
        cy = y + (lane - 1) * 46 + 18
        _boat(d, 84, cy, lane, r=18, font=f_small)
        x0, x1 = 120, W - 190
        d.rounded_rectangle([x0, cy - 13, x1, cy + 13], radius=6, fill=(38, 52, 82))
        bw = int((x1 - x0) * min(1.0, p / axis))
        if bw > 0:
            d.rounded_rectangle([x0, cy - 13, x0 + max(bw, 12), cy + 13], radius=6, fill=LANE_COLORS[lane][0],
                                outline=(210, 220, 235), width=1)
        d.text((W - 70, cy), f"{p * 100:.1f}%", font=f_small, fill=INK, anchor="rm")
    y += 6 * 46 + 30

    # スタート〜1マークの展開図
    y = _section(d, y, "スタート〜1マークの展開イメージ", f_mid)
    top, bottom = y + 6, y + 350
    d.rounded_rectangle([40, top, W - 40, bottom], radius=24, fill=WATER)
    line_x = 300
    d.text((line_x, top + 14), "スタートライン", font=f_small, fill=SUB, anchor="ma")
    for yy in range(top + 50, bottom - 16, 22):
        d.line([line_x, yy, line_x, yy + 11], fill=(230, 236, 246), width=3)
    rows = sorted(visual_rows or [], key=lambda r: r["lane"])
    for r in rows:
        lane = int(r["lane"])
        cy = top + 70 + (lane - 1) * 46
        # 想定STが早い艇ほどスタートラインに近い（差を見やすく拡大）
        cx = max(110, min(line_x - 26, int(line_x - 30 - (float(r["st"]) - 0.10) * 900)))
        _boat(d, cx, cy, lane, r=19, font=f_small)
        d.text((cx - 28, cy), f".{int(round(float(r['st']) * 100)):02d}", font=f_small, fill=SUB, anchor="rm")
    mark = (W - 120, top + 70)
    d.ellipse([mark[0] - 16, mark[1] - 16, mark[0] + 16, mark[1] + 16], fill=(250, 140, 40))
    d.text((mark[0] - 28, mark[1]), "1マーク", font=f_small, fill=SUB, anchor="rm")
    order = [int(r["lane"]) for r in sorted(rows, key=lambda r: r["turn_rank"])]
    if order:
        base_x, row1, row2 = W - 330, top + 200, top + 280
        d.text((base_x + 64, top + 140), "1マークの想定の並び", font=f_small, fill=SUB, anchor="mm")
        d.line([line_x + 30, row1, base_x - 50, row1], fill=(230, 236, 246), width=3)
        d.polygon([(base_x - 50, row1 - 10), (base_x - 30, row1), (base_x - 50, row1 + 10)], fill=(230, 236, 246))
        for i, lane in enumerate(order):
            if i < 3:
                _boat(d, base_x + i * 64, row1, lane, r=24, font=f_boat)
            else:
                _boat(d, base_x + (i - 3) * 64, row2, lane, r=19, font=f_small)
    y = bottom + 24

    # フッター
    note = f"買い目: {source_label}　" if source_label else ""
    d.text((60, y), note + "想定STは平均STと展示STから。並びは予想をもとにしたイメージです。",
           font=_font(22), fill=SUB)
    d.text((60, y + 34), "※的中や利益を保証するものではありません。舟券の購入はご自身の判断でお願いします。",
           font=_font(22), fill=SUB)
    img = img.crop((0, 0, W, y + 80))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
