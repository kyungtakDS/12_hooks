#!/bin/bash
# Pre-commit Review Hook — PreToolUse[Bash]
# Claude 가 git commit 을 실행하려 하면, 스테이징된 변경을 먼저 리뷰하고
# 심각한 문제가 있으면 커밋을 차단한다.
#
# type:"agent" 훅으로 먼저 구현했으나 한 번도 발동하지 않아 command 훅으로 바꿨다.
# command 훅은 이 저장소에서 실제 차단이 확인된 유일한 방식이다 (bash-guard 참고).
#
# 발동 조건을 명령 접두사로 보지 않는다 — `git add . && git commit -m x` 같은
# 복합 명령이 접두사 매칭을 그대로 빠져나가기 때문이다.

INPUT=$(cat)

# 기본값은 실측으로 정했다 (44KB diff 기준):
#   claude-sonnet-5 → 180초 안에 못 끝냄, claude-haiku-4-5 → 78초에 완료.
# settings.json 의 훅 타임아웃(180초)에 하드킬당하면 이유조차 못 남기므로,
# 내부 타임아웃을 그보다 짧게 잡아 우리가 메시지를 찍고 끝낸다.
REVIEW_MODEL="${PRECOMMIT_REVIEW_MODEL:-claude-haiku-4-5-20251001}"
REVIEW_TIMEOUT="${PRECOMMIT_REVIEW_TIMEOUT:-150}"
MAX_DIFF_CHARS="${PRECOMMIT_REVIEW_MAX_CHARS:-60000}"

# 재귀 방지 — 리뷰용 중첩 세션이 이 훅을 다시 돌리면 무한히 겹친다.
if [ -n "$CLAUDE_PRECOMMIT_REVIEW" ]; then
  exit 0
fi

# 페이로드 전체를 본다. jq 없이 JSON 문자열만 파싱하면 이스케이프된 따옴표에서
# 잘려 복합 명령을 놓친다. 과검사가 미검사보다 낫다 (bash-guard 와 같은 판단).
if ! printf '%s' "$INPUT" | grep -qE 'git[[:space:]]+commit'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

# 스테이징된 변경이 없으면 리뷰할 대상도 없다.
if git diff --cached --quiet 2>/dev/null; then
  exit 0
fi

DIFF=$(git diff --cached 2>/dev/null)
if [ -z "$DIFF" ]; then
  exit 0
fi

# diff 가 너무 크면 앞부분만 본다 (프롬프트 폭발 방지).
# 잘렸으면 조용히 넘어가지 않고 알린다 — 일부만 검사하고 통과시키는 것이 가장 나쁘다.
TRUNCATED=""
if [ "$(printf '%s' "$DIFF" | wc -c)" -gt "$MAX_DIFF_CHARS" ]; then
  DIFF=$(printf '%s' "$DIFF" | head -c "$MAX_DIFF_CHARS")
  TRUNCATED="yes"
  echo "PRE-COMMIT REVIEW: ⚠ diff 가 커서 뒷부분을 잘랐습니다. 잘린 범위는 검사되지 않습니다." >&2
fi

read -r -d '' PROMPT <<EOF
커밋 직전 코드 리뷰다. 아래는 이번 커밋에 스테이징된 변경(diff)이다.
이 변경이 **새로 도입하는** 심각한 문제만 찾아라.

찾을 것:
- 버그 · 크래시 · 회귀
- 보안 결함 (하드코딩된 시크릿/키, 주입, 인증·권한 우회)
- 데이터 손실 위험

무시할 것: 스타일, 네이밍, 취향, 사소한 개선, 기존 코드의 문제.

확인할 수 없는 것은 아예 언급하지 마라. 너는 아래 diff 만 볼 수 있다:
- **diff 에 없는 파일의 상태를 근거로 지적하지 마라.**
  예: ".gitattributes 에 설정이 빠졌다" — 그 파일이 diff 에 없으면
  이미 설정되어 있는지 없는지 알 수 없다. 모르면 지적하지 않는다.
- **라이브러리·액션·모델의 버전이 존재하는지 단정하지 마라.**
  예: "vN 은 존재하지 않는 버전이다" — 네 학습 시점 이후에 나온 릴리스는 알 수 없다.
- 위에 해당하면 BLOCK 하지 말고 그 항목을 통째로 빼라.
  근거 없는 차단이 반복되면 훅은 꺼지고, 그때부터 아무것도 막지 못한다.

출력 형식을 반드시 지켜라. 첫 줄에 아래 셋 중 하나만 쓴다:

- BLOCK — **하드코딩된 시크릿·자격증명이 diff 에 실제로 들어있을 때만.**
  (API 키, 비밀번호, 토큰, 커넥션 문자열 등 그 자체로 유출인 문자열)
- WARN  — 그 외의 문제를 찾았을 때 (버그·회귀·데이터 손실 위험 등)
- PASS  — 문제 없음

BLOCK 또는 WARN 이면 다음 줄부터 각 문제를 "파일:라인 — 한 줄 이유" 로 쓴다.

**BLOCK 을 시크릿 외의 이유로 쓰지 마라.** 이유: BLOCK 은 커밋을 실제로 막는다.
확신이 서지 않는 지적으로 막으면 훅이 통째로 꺼진다. 의심스러우면 WARN 을 써라.
${TRUNCATED:+
주의: diff 가 너무 커서 뒷부분이 잘렸다. 보이는 범위만 리뷰하라.}

--- staged diff ---
$DIFF
EOF

# 프롬프트는 반드시 stdin 으로 넘긴다.
# 인자로 넘기면 Windows 명령줄 한도(32767자)에 걸려
# "Argument list too long" (exit 126) 으로 죽는다. 큰 커밋일수록 확실히 걸린다.
#
# 진단은 stderr 로만 낸다 — stdout 은 Claude Code 가 파싱하는 결정 JSON 자리다.
ERR_FILE=$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/precommit-err.$$")

if command -v timeout >/dev/null 2>&1; then
  VERDICT=$(printf '%s' "$PROMPT" | CLAUDE_PRECOMMIT_REVIEW=1 \
    timeout "$REVIEW_TIMEOUT" claude -p --model "$REVIEW_MODEL" 2>"$ERR_FILE")
else
  VERDICT=$(printf '%s' "$PROMPT" | CLAUDE_PRECOMMIT_REVIEW=1 \
    claude -p --model "$REVIEW_MODEL" 2>"$ERR_FILE")
fi
RC=$?

# 리뷰 실패는 커밋을 막지 않는다 (fail open) — 훅 오류로 작업이 멈추면 안 된다.
# 다만 조용히 넘어가지는 않는다.
if [ "$RC" -ne 0 ] || [ -z "$VERDICT" ]; then
  echo "PRE-COMMIT REVIEW: ⚠ 리뷰를 완료하지 못해 검사를 건너뜁니다 (exit=$RC)." >&2
  if [ -s "$ERR_FILE" ]; then
    head -3 "$ERR_FILE" | sed 's/^/PRE-COMMIT REVIEW:   /' >&2
  fi
  rm -f "$ERR_FILE"
  exit 0
fi
rm -f "$ERR_FILE"

FIRST=$(printf '%s' "$VERDICT" | head -1)

# WARN 은 보여주되 막지 않는다. 측정된 오탐률 때문이다 — docs/ADR.md ADR-011.
# stdout 은 Claude Code 가 파싱하는 결정 JSON 자리이므로 stderr 로만 낸다.
if printf '%s' "$FIRST" | grep -q 'WARN'; then
  echo "PRE-COMMIT REVIEW: ⚠ 지적 사항 (커밋은 계속합니다)" >&2
  printf '%s\n' "$VERDICT" | sed 's/^/  /' >&2
  exit 0
fi

if ! printf '%s' "$FIRST" | grep -q 'BLOCK'; then
  exit 0
fi

# JSON 문자열에 그대로 넣을 수 있게 정리한다.
# 외부 의존성 없이 처리해야 하므로 백슬래시·따옴표를 치환하고 줄바꿈을 편다.
REASON=$(printf '%s' "$VERDICT" \
  | sed -e 's/\\/ /g' -e 's/"/'"'"'/g' \
  | tr '\n' ' ' \
  | cut -c1-1500)

cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "PRE-COMMIT REVIEW: ${REASON}"
  }
}
EOF

exit 0
