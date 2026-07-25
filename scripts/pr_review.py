#!/usr/bin/env python3
"""
GPT PR Reviewer — PR diff 를 GPT API 에 넘겨 Risk Score 와 리뷰 마크다운을 만든다.

GitHub Actions 에서 쓰지만 로컬에서도 그대로 돈다:
    git diff main... > pr.diff
    OPENAI_API_KEY=sk-... python3 scripts/pr_review.py --diff pr.diff --out review.md

--repo 와 --pr 을 주면 gh 로 PR 에 댓글까지 남긴다 (기존 리뷰 댓글이 있으면 갱신).

실패 정책은 fail-closed 다 — 리뷰를 완료하지 못하면 원인을 PR 댓글과 로그에 남기고
0 이 아닌 코드로 종료해 workflow 를 붉게 만든다. 조용히 통과시키지 않는다.
(커밋/푸시 훅은 반대로 fail-open 이다. README 참고.)

의존성은 표준 라이브러리뿐이다 — CI 에 pip install 단계를 두지 않기 위해서다.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path
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

RUBRIC_PATH = Path(__file__).resolve().parent / "risk-rubric.md"

# (JSON 키, 표에 쓸 이름, 만점) — 합이 100 이다.
CATEGORIES = (
    ("security", "보안", 30),
    ("scope", "변경 범위 및 복잡도", 20),
    ("breaking", "Breaking change", 20),
    ("tests", "테스트 및 검증", 15),
    ("ops", "마이그레이션 및 운영 영향", 15),
)

# (총점 상한, 등급, 표시) — 위에서부터 처음 맞는 구간을 쓴다.
BANDS = (
    (20, "Low", "🟢"),
    (40, "Moderate", "🟡"),
    (60, "High", "🟠"),
    (80, "Very High", "🔴"),
    (100, "Critical", "🚨"),
)

VERDICTS = ("merge 가능", "수정 후 merge", "merge 차단")


class ReviewError(RuntimeError):
    """리뷰를 완료하지 못했다. 원인을 사용자에게 보여줘야 한다."""


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

def load_rubric() -> str:
    """루브릭 파일을 읽는다. 없으면 빈 문자열 — 루브릭이 없다고 리뷰까지 막지는 않는다."""
    try:
        return RUBRIC_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


SYSTEM_TMPL = (
    "당신은 꼼꼼한 시니어 코드 리뷰어입니다. 주어진 PR diff 를 리뷰하고 {lang}(으)로 답합니다.\n"
    "\n"
    "## 리뷰 범위\n"
    "diff 에서 추가·변경된 줄(+ 로 시작하는 줄)만 리뷰합니다.\n"
    "변경되지 않은 주변 코드는 지적하지 않습니다.\n"
    "\n"
    "## 금지\n"
    "- 추측 금지. diff 에 근거가 보이지 않으면 아예 쓰지 않습니다.\n"
    "- 내용 없는 지적 금지. 다음 표현이 들어가는 항목은 그대로 삭제합니다:\n"
    "  \"검토가 필요합니다\", \"고려해야 합니다\", \"확인이 필요합니다\",\n"
    "  \"주의해야 합니다\", \"모범 사례를 따르십시오\".\n"
    "- 이미 올바르게 처리된 것을 지적하지 않습니다.\n"
    "- 지적은 최대 7개. 넘으면 심각한 것만 남깁니다.\n"
    "- 고칠 게 없으면 빈 배열로 둡니다. 억지로 만들어내지 않습니다.\n"
    "\n"
    "## 채점 기준\n"
    "아래 루브릭에 따라 항목별 점수를 매기고, 지정된 JSON 형식으로만 답합니다.\n"
    "총점과 등급은 도구가 계산하므로 직접 쓰지 않습니다.\n"
    "\n"
    "{rubric}"
)


def build_messages(diff: str, *, title: str, lang: str, truncated: bool,
                   rubric: str = "") -> list:
    """chat completions 용 messages 배열."""
    notice = "\n\n주의: diff 가 너무 커서 뒷부분이 잘렸습니다. 보이는 범위만 리뷰하세요." if truncated else ""
    user = (
        f"PR 제목: {title or '(없음)'}\n"
        f"{notice}\n\n"
        f"아래는 이 PR 의 diff 입니다.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        {"role": "system", "content": SYSTEM_TMPL.format(lang=lang, rubric=rubric)},
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
        raise ReviewError(f"OpenAI API 가 HTTP {e.code} 를 반환했습니다.\n{body}")
    except error.URLError as e:
        raise ReviewError(f"OpenAI API 에 연결하지 못했습니다 (타임아웃 포함): {e.reason}")

    choices = data.get("choices") or []
    if not choices:
        raise ReviewError(f"응답에 choices 가 없습니다: {json.dumps(data)[:500]}")

    return choices[0]["message"]["content"]


# --- 응답 파싱 & 채점 ---

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_review(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼낸다. 못 꺼내면 ValueError.

    모델이 ```json 펜스를 붙이거나 앞뒤에 설명을 다는 일이 흔해서 그것까지 걷어낸다.
    """
    if not text or not text.strip():
        raise ValueError("응답이 비어 있습니다.")

    s = text.strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()

    for candidate in (s, _brace_slice(s)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data

    raise ValueError(f"JSON 객체를 찾지 못했습니다: {text[:200]}")


def _brace_slice(s: str) -> str:
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if start != -1 and end > start else ""


def normalize_categories(data: dict) -> dict:
    """항목별 점수를 신뢰할 수 있는 형태로 만든다.

    모델이 만점을 넘기거나, 숫자가 아닌 값을 주거나, 근거 없이 점수만 매기는 경우가 있다.
    근거가 없으면 0점으로 내린다 — 루브릭의 첫 번째 원칙이다.
    """
    raw = (data or {}).get("categories") or {}
    if not isinstance(raw, dict):
        raw = {}

    out = {}
    for key, label, cap in CATEGORIES:
        item = raw.get(key)
        if not isinstance(item, dict):
            item = {}

        evidence = str(item.get("evidence") or "").strip()

        score = item.get("score", 0)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = 0
        score = int(score)
        score = max(0, min(score, cap))

        if not evidence:
            score = 0
            evidence = "문제 없음"

        out[key] = {"score": score, "evidence": evidence, "label": label, "max": cap}

    return out


def total_score(categories: dict) -> int:
    return sum(c["score"] for c in categories.values())


def risk_band(total: int) -> tuple:
    """(등급, 표시). 경계값은 구간의 상한에 포함된다 (20 → Low, 21 → Moderate)."""
    for cap, label, mark in BANDS:
        if total <= cap:
            return label, mark
    return BANDS[-1][1], BANDS[-1][2]


# --- 렌더링 ---

def _cell(text: str) -> str:
    """표 셀에 넣어도 깨지지 않게 다듬는다."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _bullets(items) -> list:
    if not isinstance(items, list):
        items = []
    clean = [str(i).strip() for i in items if str(i).strip()]
    return [f"- {c}" for c in clean] if clean else ["- 없음"]


def render_markdown(categories: dict, data: dict, *, model: str) -> str:
    """PR 댓글 본문."""
    total = total_score(categories)
    label, mark = risk_band(total)

    verdict = str((data or {}).get("verdict") or "").strip()
    if verdict not in VERDICTS:
        verdict = VERDICTS[0]

    lines = [
        MARKER,
        "## 🤖 GPT PR 리뷰",
        "",
        f"**Risk Score: {total} / 100 — {mark} {label}**",
        "",
        "| 평가 항목 | 점수 | 만점 | 주요 근거 |",
        "|---|---:|---:|---|",
    ]
    for key, cat_label, cap in CATEGORIES:
        c = categories[key]
        lines.append(f"| {cat_label} | {c['score']} | {cap} | {_cell(c['evidence'])} |")

    lines += ["", "### 주요 검토 결과", ""]
    lines += ["**발견된 문제**"] + _bullets((data or {}).get("findings")) + [""]
    lines += ["**권장 수정 사항**"] + _bullets((data or {}).get("recommendations")) + [""]
    lines += ["**잘된 점**"] + _bullets((data or {}).get("positives")) + [""]

    lines += ["### 권장 처리", "", f"- {verdict}", ""]
    lines += ["---", f"<sub>🤖 `{model}` 자동 리뷰 · 참고용이며 사람 리뷰를 대체하지 않습니다.</sub>"]

    return "\n".join(lines)


def render_error(title: str, detail: str = "") -> str:
    """리뷰를 완료하지 못했을 때의 댓글. 원인을 감추지 않는다."""
    lines = [
        MARKER,
        "## 🤖 GPT PR 리뷰",
        "",
        f"> ⚠️ **리뷰를 완료하지 못했습니다 — {title}**",
        "",
        "이 PR 은 자동 리뷰를 받지 못했습니다. 사람이 직접 확인하세요.",
    ]
    if detail:
        lines += ["", "<details><summary>원인</summary>", "", "```", str(detail)[:1500], "```", "", "</details>"]
    return "\n".join(lines)


def render_no_changes() -> str:
    return "\n".join([
        MARKER,
        "## 🤖 GPT PR 리뷰",
        "",
        "**Risk Score: 0 / 100 — 🟢 Low**",
        "",
        "리뷰할 코드 변경이 없습니다 (lockfile·바이너리만 변경됨).",
    ])


# --- PR 댓글 ---

def select_comment_id(comments: list, marker: str):
    """마커가 든 기존 리뷰 댓글의 id. 없으면 None.

    여러 개면 가장 오래된 것을 고른다 — 과거에 중복 생성된 적이 있어도
    항상 같은 댓글을 갱신하게 된다.
    """
    for c in comments or []:
        if marker in (c.get("body") or ""):
            return c.get("id")
    return None


def _gh(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True)


def post_comment(repo: str, pr: str, body: str) -> str:
    """기존 리뷰 댓글이 있으면 갱신하고, 없으면 새로 단다. 무엇을 했는지 돌려준다."""
    listed = _gh(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"])
    comments = []
    if listed.returncode == 0:
        try:
            parsed = json.loads(listed.stdout)
            if isinstance(parsed, list):
                comments = parsed
        except ValueError:
            comments = []

    cid = select_comment_id(comments, MARKER)

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        Path(path).write_text(json.dumps({"body": body}), encoding="utf-8")
        if cid:
            r = _gh(["api", "--method", "PATCH",
                     f"repos/{repo}/issues/comments/{cid}", "--input", path])
            action = f"기존 댓글 {cid} 갱신"
        else:
            r = _gh(["api", "--method", "POST",
                     f"repos/{repo}/issues/{pr}/comments", "--input", path])
            action = "새 댓글 작성"
    finally:
        os.unlink(path)

    # 댓글을 못 달았는데 workflow 가 초록으로 끝나면 fail-closed 정책이 무너진다.
    # 리뷰 결과가 아무 데도 보이지 않는데 성공으로 보이는 것이 가장 나쁘다.
    if r.returncode != 0:
        raise ReviewError(f"PR 댓글 게시 실패: {r.stderr.strip()[:300]}")
    return action


# --- 진입점 ---

def _emit(out_path: str, body: str, repo: str, pr: str) -> bool:
    """댓글 본문을 파일로 쓰고, repo/pr 이 있으면 PR 에도 올린다.

    게시에 성공했는지(또는 게시할 필요가 없었는지) 돌려준다.
    실패를 삼키면 리뷰 결과가 어디에도 남지 않은 채 workflow 가 성공으로 끝난다.
    """
    Path(out_path).write_text(body, encoding="utf-8")
    if not (repo and pr):
        return True
    try:
        print(post_comment(repo, pr, body))
        return True
    except ReviewError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="GPT 로 PR diff 를 리뷰한다")
    parser.add_argument("--diff", required=True, help="diff 파일 경로")
    parser.add_argument("--out", required=True, help="리뷰 마크다운을 쓸 경로")
    parser.add_argument("--repo", default="", help="owner/repo — 주면 PR 에 댓글까지 남긴다")
    parser.add_argument("--pr", default="", help="PR 번호")
    args = parser.parse_args()

    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    lang = os.environ.get("REVIEW_LANG", "").strip() or DEFAULT_LANG
    title = os.environ.get("PR_TITLE", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not api_key:
        msg = "OPENAI_API_KEY 가 비어 있습니다"
        print(f"ERROR: {msg}. 레포 Secrets 에 등록하세요.", file=sys.stderr)
        _emit(args.out, render_error(msg, "레포 Settings → Secrets and variables → Actions 에서 OPENAI_API_KEY 를 등록하세요."), args.repo, args.pr)
        sys.exit(1)

    raw = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    diff = filter_diff(raw)
    if not diff.strip():
        print("리뷰할 변경 없음 — API 를 호출하지 않았습니다.")
        if not _emit(args.out, render_no_changes(), args.repo, args.pr):
            sys.exit(1)
        return

    diff, truncated = truncate_diff(diff)
    messages = build_messages(diff, title=title, lang=lang, truncated=truncated,
                              rubric=load_rubric())

    print(f"모델 {model} 로 리뷰 요청 (diff {len(diff)}자, 잘림={truncated})")

    try:
        review = request_review(messages, api_key=api_key, model=model)
    except ReviewError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _emit(args.out, render_error("OpenAI API 호출 실패", str(e)), args.repo, args.pr)
        sys.exit(1)

    try:
        data = parse_review(review)
    except ValueError as e:
        print(f"ERROR: 응답을 해석하지 못했습니다: {e}", file=sys.stderr)
        _emit(args.out, render_error("모델 응답을 해석하지 못했습니다", f"{e}\n\n--- 원본 응답 ---\n{review[:1000]}"), args.repo, args.pr)
        sys.exit(1)

    categories = normalize_categories(data)
    body = render_markdown(categories, data, model=model)
    total = total_score(categories)
    label, _ = risk_band(total)
    print(f"Risk Score: {total}/100 ({label})")
    if not _emit(args.out, body, args.repo, args.pr):
        sys.exit(1)


if __name__ == "__main__":
    main()
