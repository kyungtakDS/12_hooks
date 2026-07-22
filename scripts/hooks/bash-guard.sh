#!/bin/bash
# Bash Danger Guard Hook — PreToolUse[Bash]
# 위험한 셸 명령을 차단한다.
#
# 입력은 Claude Code 가 stdin 으로 넘겨주는 JSON 이다.
# 환경변수 $CLAUDE_TOOL_INPUT 은 채워지지 않는다 — 그걸 읽으면 항상 빈 값이라
# 어떤 명령도 막지 못한다. tdd-guard.sh 와 동일하게 stdin 을 읽는다.

INPUT=$(cat)

# tool_input.command 만 뽑지 않고 페이로드 전체를 본다.
# jq 없이 JSON 문자열을 파싱하면 이스케이프된 따옴표에서 잘리는데,
# 그러면 echo "x" && rm -rf / 같은 입력이 앞부분만 검사돼 통과한다.
# 안전 가드는 과차단이 미차단보다 낫다.
DANGER='rm[[:space:]]+-rf|git[[:space:]]+push[[:space:]]+--force|git[[:space:]]+reset[[:space:]]+--hard|DROP[[:space:]]+TABLE'

if printf '%s' "$INPUT" | grep -qE "$DANGER"; then
  MATCH=$(printf '%s' "$INPUT" | grep -oE "$DANGER" | head -1)
  cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "BASH GUARD: 위험한 명령어가 감지되었습니다 ('${MATCH}'). 실행을 차단합니다."
  }
}
EOF
fi

exit 0
