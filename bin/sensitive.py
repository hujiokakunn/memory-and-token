# -*- coding: utf-8 -*-
"""秘密情報・個人情報らしき文字列の検出（記憶の add/lint と publish_check が共用）。

検出は「らしさ」の正規表現であって完全ではない。漏れはありうる前提で、公開前に人の目でも見る。
長い行での二次時間を避けるため、すべて行単位・長さ上限つきで走査する。
"""
from __future__ import annotations

import re

MAX_LINE = 4000  # これより長い行は窓に切って走査する（重なりを持たせて境界の取りこぼしを防ぐ）

# 見つけたら書き込み・公開を止めるもの（漏れたら取り消せない）
SECRET_PATTERNS: list = [
    ("Anthropic APIキー", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,200}")),
    # 英小文字だけの kebab 文字列（sk-learn-...）を拾わないよう、数字と大文字を含むものに限る
    ("OpenAI系APIキー", re.compile(r"(?<![A-Za-z0-9_\-])sk-(?:proj-|svcacct-|admin-)?(?=[\w\-]{0,200}\d)(?=[\w\-]{0,200}[A-Z])"
                                   r"[A-Za-z0-9_\-]{32,200}")),
    ("GitHubトークン", re.compile(r"(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,100}|(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{40,200}")),
    ("GitLabトークン", re.compile(r"(?<![A-Za-z0-9_])glpat-[A-Za-z0-9_\-]{20,100}")),
    ("npmトークン", re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}")),
    ("Hugging Faceトークン", re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{30,100}")),
    ("Stripeキー", re.compile(r"(?<![A-Za-z0-9_])[sr]k_live_[A-Za-z0-9]{20,200}")),
    ("AWSアクセスキー", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("Slackトークン", re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,200}")),
    ("Googleキー", re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{35}")),
    ("JWT", re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{10,2000}\.eyJ[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}")),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")),
    ("URL埋め込みの認証情報", re.compile(r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,30}://[^\s/:@]{1,200}:[^\s/@]{1,200}@")),
    ("パスワード等の代入", re.compile(r"(?i)(?<![\w])(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)"
                                     r"\s{0,3}[:=]\s{0,3}[\"'](?![<{$%])[^\s\"'<>]{8,200}[\"']")),  # 引用符で囲んだ直書きの値だけ
]

# 公開物に出してよいアドレス（完全一致）
_ALLOWED_EMAIL = re.compile(r"[0-9]+\+[A-Za-z0-9\-]+@users\.noreply\.github\.com|noreply@github\.com|"
                            r"noreply@anthropic\.com|[^@\s]+@example\.(?:com|org|net)", re.I)
# 開始位置を左境界で絞り、各部を有限長にして二次時間を避ける
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,24}(?![A-Za-z])")
# 実在のユーザー名が入った絶対パス。直前が英数字（URL のホスト名の続き＝/users/octocat 等）なら除く。
# 日本語の直後・file:///・WSL の /mnt/c/Users/ は拾う
USER_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.\-])[A-Za-z]:[\\/]{1,2}(?i:users)[\\/]{1,2}"
    r"|/mnt/[A-Za-z]/(?i:users)/"
    r"|(?<![A-Za-z0-9_.\-/:])(?:/home/|/Users/))"
    r"([A-Za-z0-9\u0080-\uffff][^\\/\s\"'`<>%$|*?:()\[\]]{0,63})")
_GENERIC_USERS = {"public", "default", "all users", "shared", "you", "user", "username", "runner", "default user"}


def _windows(text: str):
    """行単位、長い行は重なりつきの窓に切って返す。"""
    for line in text.splitlines():
        if len(line) <= MAX_LINE:
            yield line
            continue
        for i in range(0, len(line), MAX_LINE - 300):
            yield line[i:i + MAX_LINE]


def find_secrets(text: str) -> list:
    found = []
    for label, rx in SECRET_PATTERNS:
        if label not in found and any(rx.search(w) for w in _windows(text)):
            found.append(label)
    return found


def find_emails(text: str) -> list:
    hits = set()
    for w in _windows(text):
        if "@" in w:
            hits.update(m for m in EMAIL_RE.findall(w) if not _ALLOWED_EMAIL.fullmatch(m))
    return sorted(hits)


def find_user_paths(text: str) -> list:
    hits = set()
    for w in _windows(text):
        for m in USER_PATH_RE.finditer(w):
            if m.group(1).strip().lower() not in _GENERIC_USERS:
                hits.add(m.group(0))
    return sorted(hits)


def mask(s: str) -> str:
    """報告に原文を出さない（ログ・画面経由の二次漏えいを避ける）。見せるのは長さの1/4まで。"""
    n = max(1, min(2, len(s) // 10))
    return s[:n] + "…" + s[-n:] + f"（{len(s)}字）" if len(s) > 4 else "…"
