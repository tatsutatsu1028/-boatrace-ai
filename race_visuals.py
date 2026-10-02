"""AI予想画面の「確率グラフ」と「スタート〜第1ターンマークのイメージ」を作る。

予想結果（final）と出走表（work）から表示用の数値だけを取り出し、
グラフは軽いHTML（st.markdown用）、アニメーションは外部ライブラリを使わない
canvas の HTML（components.html用）として返す。公式の映像・画像は使わず、
図形はすべてここで描く。予想・買い目・購入額には一切影響しない。
"""

import html
import json
import math

import pandas as pd

# 枠番の色（1白・2黒・3赤・4青・5黄・6緑）と、その上に載せる数字の色。
LANE_COLORS = {
    1: ("#ffffff", "#111111"),
    2: ("#222222", "#ffffff"),
    3: ("#e53935", "#ffffff"),
    4: ("#1e63d6", "#ffffff"),
    5: ("#fdd835", "#111111"),
    6: ("#2e9d4a", "#ffffff"),
}

# STが取れないときの仮の値と、アニメーションで扱う範囲。
DEFAULT_ST = 0.17
MIN_ST = 0.0
MAX_ST = 0.40


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _clean_name(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def expected_st(avg_st, exhibition_st):
    """平均STと展示STから、アニメーションで使うスタートの早さを決める。

    両方あれば平均、片方だけならその値、どちらもなければ仮の値。
    展示STのフライング（負の値）は「とても早い」として0扱いにする。
    """
    values = []
    for v in (_num(avg_st), _num(exhibition_st)):
        if v is None:
            continue
        values.append(min(max(v, MIN_ST), MAX_ST))
    if not values:
        return DEFAULT_ST
    return sum(values) / len(values)


def build_visual_rows(final, work=None):
    """6艇分の表示用データ（枠番順）を作る。

    turn_rank は第1ターンマークでの並び（1着確率の高い順、0が先頭）。
    同じ確率なら内側の艇を前にする。
    """
    if final is None or len(final) == 0 or "lane" not in final.columns:
        return []

    st_cols = {}
    if work is not None and len(work) and "lane" in work.columns:
        for _, w in work.iterrows():
            lane = _num(w.get("lane"))
            if lane is None:
                continue
            st_cols[int(lane)] = (w.get("avg_st"), w.get("exhibition_st"))

    rows = []
    for _, r in final.iterrows():
        lane = _num(r.get("lane"))
        if lane is None or int(lane) not in LANE_COLORS:
            continue
        lane = int(lane)
        avg_st, ex_st = st_cols.get(lane, (r.get("avg_st"), r.get("exhibition_st")))
        rows.append({
            "lane": lane,
            "name": _clean_name(r.get("racer_name")),
            "p1": _num(r.get("p_first")),
            "p2": _num(r.get("p_second")),
            "p3": _num(r.get("p_third")),
            "avg_st": _num(avg_st),
            "ex_st": _num(ex_st),
            "st": expected_st(avg_st, ex_st),
        })

    order = sorted(rows, key=lambda x: (-(x["p1"] or 0.0), x["lane"]))
    for rank, row in enumerate(order):
        row["turn_rank"] = rank
    return sorted(rows, key=lambda x: x["lane"])


def _fmt_st(v):
    if v is None:
        return "-"
    sign = "F" if v < 0 else ""
    return f"{sign}.{int(round(abs(v) * 100)):02d}"


def probability_chart_html(rows):
    """6艇の1着（あれば2着・3着も）確率を横棒グラフにしたHTML。"""
    if not rows:
        return ""
    has_place = all(r["p2"] is not None and r["p3"] is not None for r in rows)
    values = [r["p1"] or 0.0 for r in rows]
    if has_place:
        values += [r["p2"] for r in rows] + [r["p3"] for r in rows]
    # 最大値を10%刻みで切り上げた値を棒の全幅にする（小さい確率も見えるように）。
    axis = max(0.1, math.ceil(max(values) * 10 - 1e-9) / 10)

    kinds = [("1着", "p1", 1.0, 14)]
    if has_place:
        kinds += [("2着", "p2", 0.6, 9), ("3着", "p3", 0.35, 9)]

    parts = [
        '<div style="margin:4px 0 6px 0;">',
    ]
    for r in rows:
        fill, ink = LANE_COLORS[r["lane"]]
        name = html.escape(r["name"])
        top = ' <span style="font-size:11px;opacity:0.7;">◎本命</span>' if r["turn_rank"] == 0 else ""
        parts.append(
            '<div style="display:flex;align-items:center;gap:8px;margin:8px 0 2px 0;">'
            f'<span style="display:inline-block;min-width:22px;height:22px;line-height:22px;'
            f'text-align:center;border-radius:4px;background:{fill};color:{ink};'
            'border:1px solid rgba(128,128,128,0.7);font-weight:700;font-size:13px;">'
            f'{r["lane"]}</span>'
            f'<span style="font-size:13px;font-weight:600;">{name}</span>{top}</div>'
        )
        for label, key, alpha, height in kinds:
            p = r[key] or 0.0
            width = max(0.0, min(100.0, p / axis * 100.0))
            parts.append(
                '<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
                f'<span style="width:26px;font-size:11px;opacity:0.8;">{label}</span>'
                '<div style="flex:1;background:rgba(128,128,128,0.15);border-radius:3px;'
                f'height:{height}px;overflow:hidden;">'
                f'<div style="width:{width:.1f}%;height:100%;background:{fill};opacity:{alpha};'
                'box-shadow:inset 0 0 0 1px rgba(128,128,128,0.9);border-radius:3px;"></div>'
                '</div>'
                f'<span style="width:46px;text-align:right;font-size:12px;'
                f'font-variant-numeric:tabular-nums;">{p * 100:.1f}%</span>'
                '</div>'
            )
    parts.append(
        f'<div style="font-size:11px;opacity:0.7;margin-top:6px;">'
        f'棒の右端＝{axis * 100:.0f}%。'
        + ("濃い棒が1着、薄い棒が2着・3着の確率です。" if has_place else "")
        + "</div></div>"
    )
    return "".join(parts)


def start_summary_text(rows):
    """アニメーションで使ったSTの一覧（1行）。"""
    items = []
    for r in rows:
        items.append(f"{r['lane']}号艇 {_fmt_st(r['st'])}")
    return "想定ST（平均STと展示STから）：" + " / ".join(items)


ANIMATION_HEIGHT = 310

_ANIMATION_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:transparent;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;}
  #wrap{width:100%;}
  canvas{display:block;width:100%;height:260px;border-radius:10px;}
  #bar{display:flex;align-items:center;gap:10px;margin-top:8px;}
  button{padding:8px 14px;border-radius:8px;border:1px solid #2563eb;background:#2563eb;color:#fff;font-size:14px;cursor:pointer;}
  #note{font-size:11px;color:#888;}
</style></head>
<body><div id="wrap">
<canvas id="c" role="img" aria-label="スタートから第1ターンマークまでの予想イメージ"></canvas>
<div id="bar"><button id="replay" type="button">▶ もう一度見る</button>
<span id="note">予想をもとにしたイメージです</span></div>
</div>
<script>
(function(){
  const BOATS = __BOATS__;
  const H = 260, DURATION = 7.0, T_MARK = 3.7, TURN_SPEED = 90;
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  let W = 360, raf = 0, startAt = 0, geo = null;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function layout(){
    const lineX = W * 0.30;
    // 1マークの外を一番大きく回る艇でも画面に収まるよう、マークの位置を決める。
    const R0 = 15, RSTEP = 7;
    const markX = W - 14 - (R0 + RSTEP * 5), markY = 112;
    const laneY = i => 132 + i * 23;
    const boats = BOATS.map(b => {
      const i = b.lane - 1;
      const inner = b.lane <= 3;
      // 内側(1〜3)は助走が短く、外側(4〜6)は大きく下がって助走を取る。
      const x0 = inner ? lineX - W * (0.07 + i * 0.012) : lineX - W * (0.20 + (i - 3) * 0.012);
      // 想定STが早いほど早くスタートラインを越える（差を見やすく拡大）。
      const tLine = Math.min(2.7, Math.max(1.3, 1.7 + (b.st - 0.10) * 5));
      // 1マークの並び：先頭が一番内を小さく回り、後続ほど遅れて外を大きく回る。
      const r = b.turn_rank;
      const R = R0 + RSTEP * r;
      // 旋回とその後は全艇同じ速さにして、外を回る艇ほど遅れる（並びは崩れない）。
      const tEnter = T_MARK + r * 0.2;
      const tTurn = Math.PI * R / TURN_SPEED;
      return {b, y: laneY(i), x0, tLine, R, tEnter, tTurn, a: inner ? 0.25 : 0.65};
    });
    const turnDone = Math.max(...boats.map(g => g.tEnter + g.tTurn));
    return {lineX, markX, markY, boats, turnDone};
  }

  function pos(g, t){
    const lx = geo.lineX, mx = geo.markX, my = geo.markY;
    if (t <= g.tLine){
      const u = Math.max(0, t) / g.tLine;
      return [g.x0 + (lx - g.x0) * (g.a * u + (1 - g.a) * u * u), g.y];
    }
    if (t <= g.tEnter){
      // スタートラインから1マークの入口（マークの真下、半径Rの位置）へ寄っていく。
      const s = (t - g.tLine) / (g.tEnter - g.tLine);
      const ex = mx, ey = my + g.R;
      const k = (ex - lx) * 0.4;
      const p1x = lx + k, p1y = g.y, p2x = ex - k, p2y = ey;
      const q = 1 - s;
      return [
        q * q * q * lx + 3 * q * q * s * p1x + 3 * q * s * s * p2x + s * s * s * ex,
        q * q * q * g.y + 3 * q * q * s * p1y + 3 * q * s * s * p2y + s * s * s * ey,
      ];
    }
    if (t <= g.tEnter + g.tTurn){
      // 1マークを左回り（反時計回り）に半周する。
      const u = (t - g.tEnter) / g.tTurn;
      const th = Math.PI / 2 - Math.PI * u;
      return [mx + g.R * Math.cos(th), my + g.R * Math.sin(th)];
    }
    // 回り切ったあとはバックストレッチへ向かって左へ進む。
    const d = (t - g.tEnter - g.tTurn) * TURN_SPEED;
    return [mx - d, my - g.R];
  }

  function resize(){
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(280, canvas.clientWidth || 360);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    geo = layout();
  }

  function drawWater(){
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "#1f6f9f");
    g.addColorStop(1, "#174f78");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = 1;
    for (let y = 14; y < H; y += 22){
      ctx.beginPath();
      for (let x = 0; x <= W; x += 12){
        const yy = y + Math.sin((x + y) * 0.08) * 2;
        x === 0 ? ctx.moveTo(x, yy) : ctx.lineTo(x, yy);
      }
      ctx.stroke();
    }
    // スタートライン
    ctx.setLineDash([6, 5]);
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(geo.lineX, 118); ctx.lineTo(geo.lineX, H - 12); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,255,255,0.9)";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("スタートライン", geo.lineX, H - 2);
    // 第1ターンマーク
    ctx.fillStyle = "#ff8f1f";
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(geo.markX, geo.markY, 7, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 10px sans-serif";
    ctx.fillText("1マーク", geo.markX - 30, geo.markY + 4);
  }

  function drawBoat(g, t){
    const [x, y] = pos(g, t);
    const [px, py] = pos(g, t - 0.05);
    let ang = Math.atan2(y - py, x - px);
    if (!isFinite(ang) || (Math.abs(x - px) < 1e-6 && Math.abs(y - py) < 1e-6)) ang = 0;
    // 引き波
    if (t > 0.05){
      const [wx, wy] = pos(g, t - 0.35);
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(wx, wy); ctx.lineTo(x, y); ctx.stroke();
    }
    const fill = g.b.fill, ink = g.b.ink;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(ang);
    ctx.beginPath();
    ctx.moveTo(11, 0); ctx.lineTo(4, -6); ctx.lineTo(-9, -6); ctx.lineTo(-9, 6); ctx.lineTo(4, 6);
    ctx.closePath();
    ctx.fillStyle = fill; ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.6)"; ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
    ctx.fillStyle = ink;
    ctx.font = "bold 10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(g.b.lane), x, y + 0.5);
    ctx.textBaseline = "alphabetic";
    // スタートラインを越えた直後に想定STを表示
    if (t > g.tLine && t < g.tLine + 1.4){
      ctx.globalAlpha = 1 - (t - g.tLine) / 1.4;
      ctx.fillStyle = "#ffffff";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("ST" + g.b.st_text, geo.lineX + 4, g.y - 8);
      ctx.globalAlpha = 1;
    }
  }

  function drawOrder(){
    ctx.font = "bold 11px sans-serif";
    ctx.textAlign = "left";
    const order = geo.boats.slice().sort((a, b) => a.b.turn_rank - b.b.turn_rank);
    const label = "1マーク回り切り " + order.map(g => g.b.lane).join("-");
    ctx.fillStyle = "rgba(0,0,0,0.45)";
    ctx.fillRect(6, 6, ctx.measureText(label).width + 10, 18);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(label, 11, 19);
  }

  function frame(t){
    drawWater();
    // 後ろの艇から描いて、先頭の艇を上に重ねる。
    const order = geo.boats.slice().sort((a, b) => b.b.turn_rank - a.b.turn_rank);
    for (const g of order) drawBoat(g, t);
    if (t >= geo.turnDone) drawOrder();
  }

  function tick(now){
    if (!startAt) startAt = now;
    const t = (now - startAt) / 1000;
    frame(Math.min(t, DURATION));
    if (t < DURATION) raf = requestAnimationFrame(tick);
    else raf = 0;
  }

  function play(){
    if (raf) cancelAnimationFrame(raf);
    raf = 0; startAt = 0;
    if (reduce){ frame(DURATION); return; }
    raf = requestAnimationFrame(tick);
  }

  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); if (!raf) frame(DURATION); }, 150);
  });
  document.getElementById("replay").addEventListener("click", play);
  resize();
  play();
})();
</script></body></html>
"""


def start_animation_html(rows):
    """スタートから第1ターンマークを回り切るまでを約7秒で動かす canvas の HTML。"""
    if not rows:
        return ""
    boats = []
    for r in rows:
        fill, ink = LANE_COLORS[r["lane"]]
        boats.append({
            "lane": r["lane"],
            "st": round(float(r["st"]), 3),
            "st_text": _fmt_st(r["st"]),
            "turn_rank": int(r["turn_rank"]),
            "fill": fill,
            "ink": ink,
        })
    data = json.dumps(boats, ensure_ascii=False).replace("</", "<\\/")
    return _ANIMATION_TEMPLATE.replace("__BOATS__", data)
