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
DIFF=$(printf '%s' "$DIFF" | head -c 60000)

read -r -d '' PROMPT <<EOF
커밋 직전 코드 리뷰다. 아래는 이번 커밋에 스테이징된 변경(diff)이다.
이 변경이 **새로 도입하는** 심각한 문제만 찾아라.

찾을 것:
- 버그 · 크래시 · 회귀
- 보안 결함 (하드코딩된 시크릿/키, 주입, 인증·권한 우회)
- 데이터 손실 위험

무시할 것: 스타일, 네이밍, 취향, 사소한 개선, 기존 코드의 문제.

출력 형식을 반드시 지켜라:
- 심각한 문제가 하나라도 있으면 첫 줄에 BLOCK 만 쓰고,
  다음 줄부터 각 문제를 "파일:라인 — 한 줄 이유" 형식으로 쓴다.
- 심각한 문제가 없으면 첫 줄에 PASS 만 쓰고 끝낸다.

--- staged diff ---
$DIFF
EOF

# 리뷰 실패는 커밋을 막지 않는다 (fail open) — 훅 오류로 작업이 멈추면 안 된다.
VERDICT=$(CLAUDE_PRECOMMIT_REVIEW=1 claude -p --model claude-sonnet-5 "$PROMPT" 2>/dev/null)
if [ -z "$VERDICT" ]; then
  exit 0
fi

if ! printf '%s' "$VERDICT" | head -1 | grep -q 'BLOCK'; then
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
