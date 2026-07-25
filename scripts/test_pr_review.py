"""
pr_review.py 단위 테스트.
네트워크 호출은 urlopen 을 목으로 대체해 검증한다.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pr_review as pr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SRC_CHUNK = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def f():
-    pass
+    return 1
"""

LOCK_CHUNK = """diff --git a/package-lock.json b/package-lock.json
index 3333333..4444444 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,2 +1,2 @@
-  "version": "1.0.0"
+  "version": "1.0.1"
"""

IMG_CHUNK = """diff --git a/docs/logo.png b/docs/logo.png
index 5555555..6666666 100644
Binary files a/docs/logo.png and b/docs/logo.png differ
"""


@pytest.fixture
def full_diff():
    """소스 + lockfile + 이미지가 섞인 diff."""
    return SRC_CHUNK + LOCK_CHUNK + IMG_CHUNK


# ---------------------------------------------------------------------------
# split_file_diffs
# ---------------------------------------------------------------------------

def test_split_file_diffs_파일별로_쪼갠다(full_diff):
    chunks = pr.split_file_diffs(full_diff)
    assert len(chunks) == 3
    assert all(c.startswith("diff --git ") for c in chunks)


def test_split_file_diffs_빈_입력은_빈_리스트():
    assert pr.split_file_diffs("") == []
    assert pr.split_file_diffs("   \n") == []


def test_split_file_diffs_헤더_없는_입력은_통째로_반환():
    """diff --git 헤더가 없으면 (예: 이미 가공된 텍스트) 원문을 잃지 않는다."""
    chunks = pr.split_file_diffs("어떤 텍스트\n두번째 줄\n")
    assert len(chunks) == 1
    assert "어떤 텍스트" in chunks[0]


# ---------------------------------------------------------------------------
# path_of / should_skip
# ---------------------------------------------------------------------------

def test_path_of_b측_경로를_뽑는다():
    assert pr.path_of(SRC_CHUNK) == "src/app.py"
    assert pr.path_of(LOCK_CHUNK) == "package-lock.json"


def test_path_of_헤더가_없으면_빈_문자열():
    assert pr.path_of("@@ -1 +1 @@\n+x\n") == ""


@pytest.mark.parametrize("path", [
    "package-lock.json",
    "frontend/yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "dist/bundle.min.js",
    "docs/logo.png",
    "assets/icon.svg",
])
def test_should_skip_노이즈_파일은_제외한다(path):
    assert pr.should_skip(path) is True


@pytest.mark.parametrize("path", [
    "src/app.py",
    "src/components/Button.tsx",
    "scripts/hooks/tdd-guard.sh",
    "README.md",
])
def test_should_skip_소스_파일은_통과시킨다(path):
    assert pr.should_skip(path) is False


def test_should_skip_빈_경로는_통과시킨다():
    """경로를 못 뽑은 청크를 조용히 버리면 리뷰에서 변경이 통째로 사라진다."""
    assert pr.should_skip("") is False


# ---------------------------------------------------------------------------
# filter_diff
# ---------------------------------------------------------------------------

def test_filter_diff_lockfile_과_이미지를_걷어낸다(full_diff):
    out = pr.filter_diff(full_diff)
    assert "src/app.py" in out
    assert "package-lock.json" not in out
    assert "logo.png" not in out


def test_filter_diff_전부_노이즈면_빈_문자열():
    assert pr.filter_diff(LOCK_CHUNK + IMG_CHUNK) == ""


# ---------------------------------------------------------------------------
# truncate_diff
# ---------------------------------------------------------------------------

def test_truncate_diff_한도_이내면_그대로():
    text, truncated = pr.truncate_diff("abc", limit=10)
    assert text == "abc"
    assert truncated is False


def test_truncate_diff_한도_초과면_자르고_플래그():
    text, truncated = pr.truncate_diff("a" * 50, limit=10)
    assert len(text) == 10
    assert truncated is True


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------

def test_build_messages_diff_와_제목이_담긴다():
    msgs = pr.build_messages("DIFF_BODY", title="로그인 추가", lang="한국어", truncated=False)
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "DIFF_BODY" in msgs[1]["content"]
    assert "로그인 추가" in msgs[1]["content"]
    assert "한국어" in msgs[0]["content"]


def test_build_messages_잘렸으면_그_사실을_알린다():
    msgs = pr.build_messages("D", title="t", lang="한국어", truncated=True)
    assert "잘렸" in msgs[1]["content"]


def test_build_messages_언어_설정이_반영된다():
    msgs = pr.build_messages("D", title="t", lang="English", truncated=False)
    assert "English" in msgs[0]["content"]


def _system(**kw):
    base = dict(title="t", lang="한국어", truncated=False)
    base.update(kw)
    return pr.build_messages("D", **base)[0]["content"]


def test_시스템_프롬프트가_추측을_금지한다():
    """근거 없는 지적이 실제 리뷰에서 가장 큰 노이즈였다."""
    assert "추측" in _system()


def test_루브릭에_등급_구간이_모두_있다():
    """기준 없이 점수만 요구하면 항목 배점이 들쭉날쭉해진다."""
    rubric = pr.load_rubric()
    for label in ("Low", "Moderate", "High", "Very High", "Critical"):
        assert label in rubric
    # 다섯 항목의 만점이 표에 적혀 있어야 한다
    for key, _, cap in pr.CATEGORIES:
        assert key in rubric
    assert sum(cap for _, _, cap in pr.CATEGORIES) == 100


def test_시스템_프롬프트가_루브릭을_품는다():
    sys_prompt = _system(rubric=pr.load_rubric())
    assert "security" in sys_prompt
    assert "Critical" in sys_prompt


def test_시스템_프롬프트가_내용없는_표현을_금지목록으로_준다():
    sys_prompt = _system()
    for phrase in ("검토가 필요합니다", "고려해야 합니다"):
        assert phrase in sys_prompt


def test_시스템_프롬프트에_지적_개수_상한이_있다():
    """개수 제한이 없으면 분량을 채우려고 억지 항목을 만든다."""
    assert "최대 7개" in _system()


def test_시스템_프롬프트가_추가된_줄만_보라고_지시한다():
    assert "+" in _system() and "변경되지 않은" in _system()


# ---------------------------------------------------------------------------
# Risk Score — 루브릭 로딩 / 프롬프트
# ---------------------------------------------------------------------------

def test_load_rubric_루브릭_파일을_읽는다():
    text = pr.load_rubric()
    assert "security" in text
    assert "Critical" in text


def test_build_messages_루브릭을_시스템_프롬프트에_넣는다():
    msgs = pr.build_messages("D", title="t", lang="한국어", truncated=False,
                             rubric="## 루브릭 본문 표식")
    assert "## 루브릭 본문 표식" in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Risk Score — 응답 파싱
# ---------------------------------------------------------------------------

def _payload(**over):
    cats = {
        "security": {"score": 0, "evidence": "문제 없음"},
        "scope": {"score": 2, "evidence": "README 일부 변경"},
        "breaking": {"score": 0, "evidence": "없음"},
        "tests": {"score": 5, "evidence": "신규 동작 테스트 부족"},
        "ops": {"score": 0, "evidence": "없음"},
    }
    cats.update(over.pop("categories", {}))
    data = {"categories": cats, "findings": [], "recommendations": [],
            "positives": [], "verdict": "merge 가능"}
    data.update(over)
    return data


def test_parse_review_순수_JSON():
    data = pr.parse_review(json.dumps(_payload()))
    assert data["categories"]["tests"]["score"] == 5


def test_parse_review_코드펜스로_감싼_JSON():
    """모델이 ```json 펜스를 붙이는 일이 흔하다."""
    raw = "```json\n" + json.dumps(_payload()) + "\n```"
    assert pr.parse_review(raw)["verdict"] == "merge 가능"


def test_parse_review_앞뒤_설명이_붙어도_객체를_찾는다():
    raw = "아래가 결과입니다.\n" + json.dumps(_payload()) + "\n이상입니다."
    assert pr.parse_review(raw)["categories"]["scope"]["score"] == 2


def test_parse_review_JSON_이_아니면_에러():
    with pytest.raises(ValueError):
        pr.parse_review("죄송합니다. 리뷰할 수 없습니다.")


def test_parse_review_깨진_JSON_이면_에러():
    with pytest.raises(ValueError):
        pr.parse_review('{"categories": {"security": ')


def test_parse_review_빈_응답이면_에러():
    with pytest.raises(ValueError):
        pr.parse_review("")


# ---------------------------------------------------------------------------
# Risk Score — 정규화
# ---------------------------------------------------------------------------

def test_normalize_모든_항목이_0점():
    cats = {k: {"score": 0, "evidence": "문제 없음"} for k, _, _ in pr.CATEGORIES}
    out = pr.normalize_categories({"categories": cats})
    assert pr.total_score(out) == 0
    assert all(v["score"] == 0 for v in out.values())


def test_normalize_보안_문제가_있는_경우():
    data = _payload(categories={"security": {"score": 30, "evidence": "AWS 키가 평문으로 커밋됨"}})
    out = pr.normalize_categories(data)
    assert out["security"]["score"] == 30
    assert "AWS" in out["security"]["evidence"]
    assert pr.total_score(out) == 37  # 30 + 2 + 0 + 5 + 0


def test_normalize_만점을_넘으면_깎는다():
    data = _payload(categories={"security": {"score": 999, "evidence": "시크릿 노출"}})
    assert pr.normalize_categories(data)["security"]["score"] == 30


def test_normalize_음수는_0으로():
    data = _payload(categories={"scope": {"score": -5, "evidence": "x"}})
    assert pr.normalize_categories(data)["scope"]["score"] == 0


def test_normalize_근거가_없으면_0점():
    """근거 없이 점수만 높게 주는 것을 막는다 (루브릭 원칙 1)."""
    data = _payload(categories={"security": {"score": 25, "evidence": "   "}})
    assert pr.normalize_categories(data)["security"]["score"] == 0


def test_normalize_점수가_숫자가_아니면_0점():
    data = _payload(categories={"ops": {"score": "높음", "evidence": "배포 영향"}})
    assert pr.normalize_categories(data)["ops"]["score"] == 0


def test_normalize_누락된_항목은_0점으로_채운다():
    out = pr.normalize_categories({"categories": {"security": {"score": 3, "evidence": "사소"}}})
    assert set(out) == {k for k, _, _ in pr.CATEGORIES}
    assert out["ops"]["score"] == 0


def test_normalize_categories_키가_아예_없어도_깨지지_않는다():
    out = pr.normalize_categories({})
    assert pr.total_score(out) == 0


# ---------------------------------------------------------------------------
# Risk Score — 등급 경계값
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("total,label", [
    (0, "Low"), (20, "Low"),
    (21, "Moderate"), (40, "Moderate"),
    (41, "High"), (60, "High"),
    (61, "Very High"), (80, "Very High"),
    (81, "Critical"), (100, "Critical"),
])
def test_risk_band_경계값(total, label):
    assert pr.risk_band(total)[0] == label


def test_risk_band_등급마다_다른_표시():
    marks = {pr.risk_band(t)[1] for t in (0, 30, 50, 70, 90)}
    assert len(marks) == 5


# ---------------------------------------------------------------------------
# Risk Score — Markdown 렌더링
# ---------------------------------------------------------------------------

def test_render_markdown_표와_점수를_만든다():
    data = _payload(findings=["a.py:1 — 문제"], recommendations=["고쳐라"],
                    positives=["테스트 좋음"], verdict="수정 후 merge")
    cats = pr.normalize_categories(data)
    md = pr.render_markdown(cats, data, model="gpt-4o")

    assert pr.MARKER in md
    assert "## 🤖 GPT PR 리뷰" in md
    assert "**Risk Score: 7 / 100" in md
    assert "🟢 Low" in md
    # 표 헤더와 다섯 항목이 모두 있어야 한다
    assert "| 평가 항목 | 점수 | 만점 | 주요 근거 |" in md
    for _, label, cap in pr.CATEGORIES:
        assert f"| {label} |" in md
        assert f"| {cap} |" in md
    assert "| 보안 | 0 | 30 | 문제 없음 |" in md
    assert "### 주요 검토 결과" in md
    assert "a.py:1 — 문제" in md
    assert "### 권장 처리" in md
    assert "수정 후 merge" in md
    assert "gpt-4o" in md


def test_render_markdown_비어있는_목록도_표시된다():
    data = _payload()
    md = pr.render_markdown(pr.normalize_categories(data), data, model="m")
    assert "### 주요 검토 결과" in md
    assert "없음" in md


def test_render_markdown_표의_파이프를_이스케이프한다():
    """근거에 | 가 들어가면 표가 깨진다."""
    data = _payload(categories={"ops": {"score": 1, "evidence": "a | b"}})
    md = pr.render_markdown(pr.normalize_categories(data), data, model="m")
    assert "a \\| b" in md


def test_render_error_원인을_남긴다():
    md = pr.render_error("OPENAI_API_KEY 없음", "Secrets 에 등록하세요")
    assert pr.MARKER in md
    assert "OPENAI_API_KEY 없음" in md
    assert "Secrets 에 등록하세요" in md


# ---------------------------------------------------------------------------
# 기존 PR 댓글 업데이트
# ---------------------------------------------------------------------------

def test_select_comment_id_마커가_있는_댓글을_찾는다():
    comments = [
        {"id": 1, "body": "사람이 쓴 댓글"},
        {"id": 2, "body": pr.MARKER + "\n이전 리뷰"},
        {"id": 3, "body": "또 다른 댓글"},
    ]
    assert pr.select_comment_id(comments, pr.MARKER) == 2


def test_select_comment_id_없으면_None():
    assert pr.select_comment_id([{"id": 1, "body": "x"}], pr.MARKER) is None


def test_select_comment_id_빈_목록():
    assert pr.select_comment_id([], pr.MARKER) is None


def test_select_comment_id_여러개면_가장_오래된_것():
    comments = [
        {"id": 5, "body": pr.MARKER + " 첫 리뷰"},
        {"id": 9, "body": pr.MARKER + " 중복 리뷰"},
    ]
    assert pr.select_comment_id(comments, pr.MARKER) == 5


# ---------------------------------------------------------------------------
# request_review
# ---------------------------------------------------------------------------

def _fake_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def test_request_review_본문을_반환한다():
    fake = _fake_response({"choices": [{"message": {"content": "리뷰 내용"}}]})
    with patch.object(pr.request, "urlopen", return_value=fake):
        out = pr.request_review([{"role": "user", "content": "x"}],
                                api_key="sk-test", model="gpt-4o")
    assert out == "리뷰 내용"


def test_request_review_인증_헤더와_모델을_담아_보낸다():
    fake = _fake_response({"choices": [{"message": {"content": "ok"}}]})
    with patch.object(pr.request, "urlopen", return_value=fake) as m:
        pr.request_review([{"role": "user", "content": "x"}],
                          api_key="sk-test", model="my-model")

    req = m.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer sk-test"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "my-model"
    # 모델마다 지원 파라미터가 달라 최소 페이로드만 보낸다.
    assert set(body.keys()) == {"model", "messages"}


def test_request_review_응답이_비면_에러():
    """ReviewError 로 올린다 — main 이 잡아서 PR 댓글에 원인을 남겨야 하기 때문이다."""
    fake = _fake_response({"choices": []})
    with patch.object(pr.request, "urlopen", return_value=fake):
        with pytest.raises(pr.ReviewError):
            pr.request_review([{"role": "user", "content": "x"}],
                              api_key="sk-test", model="gpt-4o")


def test_request_review_HTTP_에러도_ReviewError():
    err = pr.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))
    with patch.object(pr.request, "urlopen", side_effect=err):
        with pytest.raises(pr.ReviewError) as ei:
            pr.request_review([{"role": "user", "content": "x"}],
                              api_key="sk-bad", model="gpt-4o")
    assert "401" in str(ei.value)


def test_request_review_연결_실패도_ReviewError():
    with patch.object(pr.request, "urlopen", side_effect=pr.error.URLError("timed out")):
        with pytest.raises(pr.ReviewError):
            pr.request_review([{"role": "user", "content": "x"}],
                              api_key="sk-test", model="gpt-4o")
