"""
アプリの処理時間の計測（ログに出すだけ。予想・表示の中身は変えない）。

  - 1回の実行（ページを開く・ボタンを押す・再実行）ごとに、区間ごとの CPU 時間と経過時間を集め、
    最後に "[CPU] ..." の1行を Streamlit の Logs に出す（CPU の多い順）
  - 予想ボタンの段階ごとの時間を "[TIMING] ..." の1行で出す
  - 予想ボタンの処理が上限（120秒）を超えたら、次の区間に入るところで PredictionTimeout を出して打ち切る

CPU 時間は time.thread_time()（今の実行のスレッドだけ。Streamlit は1回の実行を1つのスレッドで動かす）。
他の人の実行や、通信の待ち時間は入らない。「プロセス全体CPU」は同じ間のプロセス全体（裏のスレッド・
他の人の実行も含む）。区間ごとの数字は「CPU/経過」。区間を入れ子にすると、外側の区間からは内側の時間を引く（重ねて数えない）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_local = threading.local()


class PredictionTimeout(RuntimeError):
    """予想ボタンの処理が上限時間を超えた。"""


class _Recorder:
    def __init__(self, label, deadline=None):
        self.label = label
        self.t0 = time.perf_counter()
        self.c0 = time.thread_time()
        self.p0 = time.process_time()
        self.items = {}  # name -> [wall, cpu, count]
        self.stack = []  # [name, wall0, cpu0, child_wall, child_cpu]
        self.deadline = deadline

    def check(self):
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise PredictionTimeout(f"{self.label}が {self.limit:.0f}秒を超えたため打ち切りました")

    def enter(self, name, lap=False):
        self.stack.append([name, time.perf_counter(), time.thread_time(), 0.0, 0.0, lap])

    def exit(self):
        name, w0, c0, cw, cc, _lap = self.stack.pop()
        wall, cpu = time.perf_counter() - w0, time.thread_time() - c0
        it = self.items.setdefault(name, [0.0, 0.0, 0])
        it[0] += wall - cw
        it[1] += cpu - cc
        it[2] += 1
        if self.stack:
            self.stack[-1][3] += wall
            self.stack[-1][4] += cpu

    def totals(self):
        return time.perf_counter() - self.t0, time.thread_time() - self.c0


def _stack():
    s = getattr(_local, "recorders", None)
    if s is None:
        s = _local.recorders = []
    return s


@contextmanager
def section(name):
    """区間を測る。計測中でなければ何もしない。予想の計測中なら、区間に入るところで上限時間を確かめる
    （区間の途中では止めない。保存などを途中で止めないよう、出るところでは確かめない）。"""
    recs = list(_stack())
    for r in recs:
        r.check()
    for r in recs:
        r.enter(name)
        listener = getattr(r, "listener", None)
        if listener is not None:
            try:
                listener(name)
            except Exception:  # noqa: BLE001  表示の失敗で処理は止めない
                pass
    try:
        yield
    finally:
        for r in recs:
            try:
                r.exit()
            except IndexError:
                pass


def lap(name):
    """ここから次の lap（または計測の終わり）までを name として測る（上から順に流れる処理の区切り）。
    区間（section）の中では何もしない。"""
    for r in _stack():
        if r.stack and not (len(r.stack) == 1 and r.stack[0][5]):
            continue
        if r.stack:
            r.exit()
        r.enter(name, lap=True)


def check_deadline():
    for r in _stack():
        r.check()


def start(label, limit_seconds=None):
    """計測を始める（入れ子にできる。予想ボタンの計測はページの計測の中で始める）。"""
    rec = _Recorder(label)
    if limit_seconds:
        rec.limit = float(limit_seconds)
        rec.deadline = rec.t0 + float(limit_seconds)
    _stack().append(rec)
    return rec


def finish(rec, tag="[CPU]", order=None, min_seconds=0.0, extra=""):
    """計測を終えて1行をログに出す。order を渡すとその順で、無ければ CPU の多い順。戻り値は出した文字列。"""
    s = _stack()
    while rec.stack:
        rec.exit()
    if rec in s:
        s.remove(rec)
    wall, cpu = rec.totals()
    items = dict(rec.items)
    known_w = sum(v[0] for v in items.values())
    known_c = sum(v[1] for v in items.values())
    names = list(order) if order else sorted(items, key=lambda k: -items[k][1])
    parts = []
    for n in names:
        if n not in items:
            continue
        w, c, k = items[n]
        if order or c >= min_seconds or w >= min_seconds:
            parts.append(f"{n}={c:.2f}/{w:.2f}s" + (f"x{k}" if k > 1 else ""))
    rest_c, rest_w = max(cpu - known_c, 0.0), max(wall - known_w, 0.0)
    parts.append(f"その他={rest_c:.2f}/{rest_w:.2f}s")
    proc = time.process_time() - rec.p0
    line = (f"{tag} {rec.label} CPU {cpu:.2f}s / 経過 {wall:.2f}s / プロセス全体CPU {proc:.2f}s{extra} | "
            + " ".join(parts))
    print(line, flush=True)
    return line


def stage_text(rec, order):
    """画面に出す段階ごとの時間（経過時間。回数が2回以上なら回数も）。"""
    parts = []
    for n in order:
        if n in rec.items:
            w, _c, k = rec.items[n]
            parts.append(f"{n} {w:.1f}秒" + (f"（{k}回）" if k > 1 else ""))
    return " / ".join(parts)


def abandon_all():
    """途中で止まった計測を捨てる（次の実行に持ち越さない）。"""
    _local.recorders = []
