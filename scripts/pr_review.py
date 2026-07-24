#!/usr/bin/env python3
"""
GPT PR Reviewer — PR diff 를 GPT API 에 넘겨 리뷰 마크다운을 만든다.

GitHub Actions 에서 쓰지만 로컬에서도 그대로 돈다:
    git diff main... > pr.diff
    OPENAI_API_KEY=sk-... python3 scripts/pr_review.py --diff pr.diff --out review.md

의존성은 표준 라이브러리뿐이다 — CI 에 pip install 단계를 두지 않기 위해서다.
"""

import argparse
import json
import os
import re
import sys
from fnmatch import fnmatch
from urllib import error, request

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o"
DEFAULT_LANG = "한국어"

# diff 를 통째로 넘기면 토큰이 폭발한다. 문자 기준 상한 (대략 1/4 이 토큰).
MAX_DIFF_CHARS = 60000

# 리뷰할 가치가 없는데 diff 만 크게 만드는 파일들.
SKIP_NAMES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "composer.lock", "go.sum",
)
SKIP_GLOBS = (
    "*.min.js", "*.min.css", "*.map",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp", "*.svg",
    "*.pdf", "*.zip", "*.woff", "*.woff2", "*.ttf",
)

_HEADER_RE = re.compile(r"(?m)^diff --git a/(.+?) b/(.+)$")

MARKER = "<!-- gpt-pr-review -->"


# --- diff 가공 ---

def split_file_diffs(diff: str) -> list:
    """diff 를 파일 단위 청크로 쪼갠다. 헤더가 없으면 원문을 한 덩어리로 돌려준다."""
    if not diff.strip():
        return []
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    return [p for p in parts if p.strip()]


def path_of(chunk: str) -> str:
    """청크에서 변경 후(b/) 경로를 뽑는다. 못 찾으면 빈 문자열."""
    m = _HEADER_RE.search(chunk)
    return m.group(2).strip() if m else ""


def should_skip(path: str) -> bool:
    """리뷰 대상에서 제외할 파일인지. 경로를 못 뽑았으면 버리지 않는다."""
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    if name in SKIP_NAMES:
        return True
    return any(fnmatch(name, g) for g in SKIP_GLOBS)


def filter_diff(diff: str) -> str:
    """lockfile·바이너리 청크를 걷어낸 diff."""
    kept = [c for c in split_file_diffs(diff) if not should_skip(path_of(c))]
    return "".join(kept)


def truncate_diff(diff: str, limit: int = MAX_DIFF_CHARS) -> tuple:
    """(잘린 diff, 잘렸는지 여부)."""
    if len(diff) <= limit:
        return diff, False
    return diff[:limit], True


# --- 프롬프트 ---

SYSTEM_TMPL = (
    "당신은 꼼꼼한 시니어 코드 리뷰어입니다. 주어진 PR diff 를 리뷰하고 {lang}(으)로 답합니다.\n"
    "\n"
    "우선순위대로 봅니다:\n"
    "1. 버그 — 잘못된 로직, 처리되지 않은 예외, 경계 조건, 널/타입 오류\n"
    "2. 보안 — 주입, 시크릿 노출, 권한 검사 누락\n"
    "3. 설계 — 불필요한 복잡도, 중복, 누락된 테스트\n"
    "4. 사소한 것 — 네이밍, 스타일\n"
    "\n"
    "규칙:\n"
    "- diff 에 실제로 보이는 것만 지적합니다. 추측하지 않습니다.\n"
    "- 지적마다 `파일:줄` 을 붙이고, 왜 문제인지 한 문장으로 설명합니다.\n"
    "- 고칠 게 없으면 없다고 말합니다. 억지로 만들어내지 않습니다.\n"
    "- 칭찬이나 요약으로 분량을 채우지 않습니다.\n"
    "\n"
    "형식(마크다운):\n"
    "## 요약\n"
    "한두 문장.\n"
    "## 지적사항\n"
    "심각도(🔴 높음 / 🟡 보통 / 🟢 낮음)를 앞에 붙인 목록. 없으면 '없음'."
)


def build_messages(diff: str, *, title: str, lang: str, truncated: bool) -> list:
    """chat completions 용 messages 배열."""
    notice = "\n\n주의: diff 가 너무 커서 뒷부분이 잘렸습니다. 보이는 범위만 리뷰하세요." if truncated else ""
    user = (
        f"PR 제목: {title or '(없음)'}\n"
        f"{notice}\n\n"
        f"아래는 이 PR 의 diff 입니다.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        {"role": "system", "content": SYSTEM_TMPL.format(lang=lang)},
        {"role": "user", "content": user},
    ]


# --- API 호출 ---

def request_review(messages: list, *, api_key: str, model: str, url: str = API_URL) -> str:
    """GPT 를 호출해 리뷰 본문을 돌려준다.

    페이로드는 model 과 messages 만 담는다 — temperature/max_tokens 는 모델마다
    지원 여부와 이름이 달라, 넣으면 모델을 바꿀 때 400 으로 깨진다.
    """
    payload = json.dumps({"model": model, "messages": messages}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:1000]
        sys.exit(f"ERROR: OpenAI API 가 {e.code} 를 반환했습니다.\n{body}")
    except error.URLError as e:
        sys.exit(f"ERROR: OpenAI API 에 연결하지 못했습니다: {e.reason}")

    choices = data.get("choices") or []
    if not choices:
        sys.exit(f"ERROR: 응답에 choices 가 없습니다: {json.dumps(data)[:500]}")

    return choices[0]["message"]["content"]


# --- 진입점 ---

def main():
    parser = argparse.ArgumentParser(description="GPT 로 PR diff 를 리뷰한다")
    parser.add_argument("--diff", required=True, help="diff 파일 경로")
    parser.add_argument("--out", required=True, help="리뷰 마크다운을 쓸 경로")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        sys.exit("ERROR: OPENAI_API_KEY 가 비어 있습니다. 레포 Secrets 에 등록하세요.")

    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    lang = os.environ.get("REVIEW_LANG", "").strip() or DEFAULT_LANG
    title = os.environ.get("PR_TITLE", "").strip()

    with open(args.diff, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    diff = filter_diff(raw)
    if not diff.strip():
        body = f"{MARKER}\n## 요약\n리뷰할 코드 변경이 없습니다 (lockfile·바이너리만 변경됨)."
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print("리뷰할 변경 없음 — API 를 호출하지 않았습니다.")
        return

    diff, truncated = truncate_diff(diff)
    messages = build_messages(diff, title=title, lang=lang, truncated=truncated)

    print(f"모델 {model} 로 리뷰 요청 (diff {len(diff)}자, 잘림={truncated})")
    review = request_review(messages, api_key=api_key, model=model)

    body = f"{MARKER}\n{review}\n\n---\n<sub>🤖 `{model}` 자동 리뷰 · 참고용이며 사람 리뷰를 대체하지 않습니다.</sub>"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(body)

    print(f"리뷰 {len(review)}자를 {args.out} 에 썼습니다.")


if __name__ == "__main__":
    main()
