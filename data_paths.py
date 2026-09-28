"""
学習・過去データの置き場所。

公式データを公開リポジトリで再配布しないよう、データは非公開リポジトリ
（既定: tatsutatsu1028/-boatrace-ai-data）に置き、このリポジトリには置かない。

- GitHub Actions: 非公開リポジトリを _data/ に取り出し、環境変数
  BOATRACE_DATA_DIR にその場所を渡す（各ワークフローの Checkout data 参照）
- Streamlitアプリ: 起動時に sync_from_github() で読み取り専用トークンを使って
  一時フォルダへダウンロードし、BOATRACE_DATA_DIR をそこに向ける
- 手元での実行: BOATRACE_DATA_DIR が無ければリポジトリ直下を見る
  （従来どおり、同じ場所にCSVを置けば動く）

データ用リポジトリの中身（リポジトリ直下からの相対パス）:
  sample_history.csv / history_full.csv / data/history/...
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import requests

ENV_NAME = "BOATRACE_DATA_DIR"
DEFAULT_REPO = "tatsutatsu1028/-boatrace-ai-data"
# Streamlitアプリが学習に使うファイル。data/history/ はモデル作り直し用で
# アプリは読まないのでダウンロードしない。
APP_FILES = ("sample_history.csv", "history_full.csv")

_CODE_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    """データの置き場所。呼ぶたびに環境変数を読む（起動後の切り替えに追従する）。"""
    value = os.environ.get(ENV_NAME, "").strip()
    return Path(value).resolve() if value else _CODE_DIR


def data_path(name: str) -> Path:
    return data_dir() / name


def _headers(token: str, accept: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _remote_sha(repo, path, token, ref, timeout):
    """ファイルのblob SHA。1MBを超えるファイルでもメタデータは返る。"""
    r = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_headers(token, "application/vnd.github.object+json"),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["sha"]


def _download(repo, path, token, ref, dest, timeout):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_headers(token, "application/vnd.github.raw+json"),
        timeout=timeout,
        stream=True,
    )
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 読み込み中の別セッションが途中のファイルを読まないよう、書き終えてから置き換える
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sync_from_github(token, repo=DEFAULT_REPO, files=APP_FILES, dest=None,
                     ref="main", timeout=60):
    """
    非公開リポジトリのファイルを dest にそろえ、BOATRACE_DATA_DIR を dest に向ける。

    変わっていないファイル（blob SHAが同じ）は取り直さない。取り直した
    ファイルだけ更新日時が変わるので、アプリ側の学習キャッシュ
    （更新日時とサイズがキー）は中身が変わったときだけ作り直される。

    戻り値: {"dir": str, "updated": [...], "kept": [...], "errors": {...}}
    取得に失敗したファイルは、前回ダウンロード済みのものがあればそれを使う。
    """
    dest = Path(dest or Path(tempfile.gettempdir()) / "boatrace_data")
    dest.mkdir(parents=True, exist_ok=True)
    state_file = dest / ".sha.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    result = {"dir": str(dest), "updated": [], "kept": [], "errors": {}}
    for name in files:
        local = dest / name
        try:
            sha = _remote_sha(repo, name, token, ref, timeout)
            if local.exists() and state.get(name) == sha:
                result["kept"].append(name)
                continue
            _download(repo, name, token, ref, local, timeout)
            state[name] = sha
            result["updated"].append(name)
        except Exception as e:  # noqa: BLE001
            # トークンは例外メッセージに含まれないが、念のため種類と状態コードだけ残す
            status = getattr(getattr(e, "response", None), "status_code", None)
            result["errors"][name] = f"{type(e).__name__}{f' {status}' if status else ''}"

    try:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass

    if all((dest / name).exists() for name in files):
        os.environ[ENV_NAME] = str(dest)
    return result
