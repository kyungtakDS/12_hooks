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


def test_시스템_프롬프트에_심각도_기준이_있다():
    """기준 없이 이모지만 주면 사소한 것도 🔴 로 붙는다."""
    sys_prompt = _system()
    for mark in ("🔴", "🟡", "🟢"):
        assert mark in sys_prompt
    assert "재현" in sys_prompt


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
    fake = _fake_response({"choices": []})
    with patch.object(pr.request, "urlopen", return_value=fake):
        with pytest.raises(SystemExit):
            pr.request_review([{"role": "user", "content": "x"}],
                              api_key="sk-test", model="gpt-4o")
