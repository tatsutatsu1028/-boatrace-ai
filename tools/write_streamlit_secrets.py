"""
GitHub Actions の Secrets（環境変数）から .streamlit/secrets.toml を作る（predict_race.yml）。

アプリの関数（result_tracker・claude_usage など）は Streamlit の st.secrets から Supabase の接続先を読むので、
Actions でも同じ関数をそのまま使えるようにする。値はログに出さない。

  python tools/write_streamlit_secrets.py .streamlit/secrets.toml
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _q(value):
    # TOML の基本文字列は JSON の文字列と同じ書き方で書ける（\\ と " と制御文字をエスケープ）
    return json.dumps(str(value), ensure_ascii=False)


def build(env):
    url = env.get("SUPABASE_URL", "").strip().rstrip("/")
    key = env.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_KEY がありません")
    lines = []
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_READ_MODEL"):
        value = env.get(name, "").strip()
        if value:
            lines.append(f"{name} = {_q(value)}")
    lines += ["", "[supabase]", f"url = {_q(url)}", f"key = {_q(key)}", ""]
    return "\n".join(lines)


def main(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build(os.environ), encoding="utf-8")
    os.chmod(p, 0o600)
    print(f"[SECRETS] {p} を作りました", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".streamlit/secrets.toml")
