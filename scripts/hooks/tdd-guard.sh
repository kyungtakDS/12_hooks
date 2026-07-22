#!/bin/bash
# TDD Guard Hook — PreToolUse[Edit|Write]
# 구현 코드를 작성하려 할 때, 해당 모듈의 테스트 파일이 먼저 존재하는지 체크.
# 테스트 없이 구현 코드를 작성하려 하면 차단.

INPUT=$(cat)

# tool_input.file_path 만 필요하므로 jq 없이 추출한다 (외부 의존성 없음).
# 첫 번째 "file_path" 매치를 쓴다 — Write 의 content 안에 같은 문자열이 들어와도
# 실제 키가 앞에 오므로 안전하다.
# JSON 이스케이프된 백슬래시는 / 로 바꾼다 (Windows 경로도 아래 glob 들이 매치하도록).
FILE_PATH=$(printf '%s' "$INPUT" \
  | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' \
  | head -1 \
  | sed -e 's/^"file_path"[[:space:]]*:[[:space:]]*"//' -e 's/"$//' \
        -e 's|\\\\|/|g' -e 's|\\/|/|g' -e 's/\\"/"/g')

# 파일 경로가 없으면 통과
if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# 테스트 파일 자체를 수정하는 건 허용
# 파일명 경계를 지키는 패턴만 쓴다 — *test* 같은 넓은 패턴은 latest.ts, contest/ 처럼
# 무관한 경로까지 통과시켜 훅을 무력화한다.
case "$FILE_PATH" in
  *.test.*|*.spec.*|*/__tests__/*)
    exit 0
    ;;
esac

# 설정/타입/스타일 파일은 테스트 불필요 — 허용
case "$FILE_PATH" in
  *.json|*.css|*.scss|*.md|*.yml|*.yaml|*.env*|*.config.*|*tailwind*|*postcss*|*next.config*|*tsconfig*)
    exit 0
    ;;
esac

# types/ 폴더는 테스트 불필요 — 허용
case "$FILE_PATH" in
  */types/*|*/types.ts|*/types.d.ts)
    exit 0
    ;;
esac

# Next.js 프레임워크 파일은 허용 (layout, page, loading, error, not-found, global styles)
case "$FILE_PATH" in
  */layout.tsx|*/layout.ts|*/page.tsx|*/page.ts|*/loading.tsx|*/error.tsx|*/not-found.tsx|*/globals.css)
    exit 0
    ;;
esac

# 주석과 문자열 리터럴을 걷어낸다 — 그 안에 든 it( 로 우회하는 것을 막기 위해서다.
# 한 글자씩 훑는 스캐너. 정규식 리터럴도 상태로 다루는데, 이걸 빼면
# /['"]/ 같은 흔한 정규식의 따옴표가 스캐너를 깨뜨려 멀쩡한 테스트를 오탐한다.
# 작은따옴표는 셸 인용과 충돌하므로 awk 8진 이스케이프 \047 로 쓴다.
strip_comments_and_strings() {
  awk '
    { src = src $0 "\n" }
    END {
      n = length(src); state = "code"; prev = ""; out = ""
      for (i = 1; i <= n; i++) {
        c = substr(src, i, 1); d = substr(src, i + 1, 1)
        if (state == "code") {
          if (c == "/" && d == "/") { state = "line"; i++ }
          else if (c == "/" && d == "*") { state = "block"; i++ }
          else if (c == "\"") { state = "dq"; out = out " " }
          else if (c == "\047") { state = "sq"; out = out " " }
          else if (c == "`") { state = "tpl"; out = out " " }
          else if (c == "/" && (prev == "" || index("(,=:[!&|?{};+-*%<>~^", prev) > 0)) { state = "re"; out = out " " }
          else { out = out c; if (c != " " && c != "\t" && c != "\n") prev = c }
        }
        else if (state == "line") { if (c == "\n") { state = "code"; out = out "\n" } }
        else if (state == "block") {
          if (c == "*" && d == "/") { state = "code"; i++; out = out " " }
          else if (c == "\n") out = out "\n"
        }
        else {
          if (c == "\\") i++
          else if ((state == "dq" && c == "\"") || (state == "sq" && c == "\047") ||
                   (state == "tpl" && c == "`") || (state == "re" && c == "/")) { state = "code"; prev = "x" }
          # 템플릿 리터럴만 줄바꿈을 넘을 수 있다. 나머지는 미종료로 보고 복구한다.
          else if (c == "\n") { out = out "\n"; if (state != "tpl") state = "code" }
        }
      }
      print out
    }
  ' "$1"
}

# 실행되는 테스트 케이스가 하나라도 있으면 종료코드 0.
# - it(...) / test(...) 만 인정. it.skip / it.todo / xit 은 제외.
# - describe.skip / xdescribe 블록 안의 it() 도 제외 — 하위 전체가 실행되지 않으므로.
# 괄호 깊이로 블록 포함 관계를 본다. 주석과 문자열이 이미 제거된 입력이라 괄호를 신뢰할 수 있다.
has_live_test_case() {
  awk '
    # "(" 바로 앞의 식별자 체인을 뒤로 훑어 읽는다 (it, it.skip.each, re.test ...).
    function chain_before(s, pos,   j, ch) {
      j = pos - 1
      while (j >= 1 && substr(s, j, 1) ~ /[ \t\n]/) j--
      ch = ""
      while (j >= 1 && substr(s, j, 1) ~ /[A-Za-z0-9_$.]/) { ch = substr(s, j, 1) ch; j-- }
      return ch
    }
    function enclosed_in_skip(   k) {
      for (k = 1; k <= sp; k++) if (stack[k]) return 1
      return 0
    }
    { src = src $0 "\n" }
    END {
      n = length(src); sp = 0
      for (i = 1; i <= n; i++) {
        c = substr(src, i, 1)
        if (c == "(") {
          ch = chain_before(src, i)
          base = ch; sub(/\..*$/, "", base)
          skipped = (ch ~ /(^|\.)(skip|todo)($|\.)/)
          # 스킵된 describe 안에 있지 않은 진짜 케이스를 만나면 즉시 성공.
          if ((base == "it" || base == "test") && !skipped && !enclosed_in_skip()) exit 0
          sp++
          if ((((base == "describe" || base == "suite")) && skipped) || base == "xdescribe") stack[sp] = 1
          else stack[sp] = 0
        }
        else if (c == ")") { if (sp > 0) sp-- }
      }
      exit 1
    }
  '
}

# lib/ 또는 소스 파일이면 테스트 파일 존재 여부 확인
case "$FILE_PATH" in
  *.ts|*.tsx|*.js|*.jsx)
    # 파일명 추출 — 확장자는 안내 메시지에 그대로 쓴다 (.js 프로젝트에 .ts 를 권하지 않도록).
    DIR=$(dirname "$FILE_PATH")
    BASENAME=$(basename "$FILE_PATH" | sed -E 's/\.(ts|tsx|js|jsx)$//')
    SRC_EXT=$(basename "$FILE_PATH" | sed -E 's/^.*\.(ts|tsx|js|jsx)$/\1/')

    # 찾은 테스트 파일 경로 — 내용까지 확인해야 하므로 경로를 기억한다.
    TEST_FILE=""

    # 같은 폴더에 .test / .spec 파일
    for EXT in ts tsx js jsx; do
      for SUF in test spec; do
        if [ -f "${DIR}/${BASENAME}.${SUF}.${EXT}" ]; then
          TEST_FILE="${DIR}/${BASENAME}.${SUF}.${EXT}"
          break 2
        fi
      done
    done

    # __tests__ 폴더
    if [ -z "$TEST_FILE" ]; then
      PARENT=$(dirname "$DIR")
      for EXT in ts tsx js jsx; do
        for TDIR in "${PARENT}/__tests__" "${DIR}/__tests__"; do
          if [ -f "${TDIR}/${BASENAME}.test.${EXT}" ]; then
            TEST_FILE="${TDIR}/${BASENAME}.test.${EXT}"
            break 2
          fi
        done
      done
    fi

    # src/__tests__/ 루트 테스트 폴더
    # CLAUDE_PROJECT_DIR 를 먼저 쓴다. git 저장소가 아니면 예전엔 "." 로 떨어져
    # 훅의 실행 CWD 에 따라 같은 입력이 다르게 판정됐다.
    if [ -z "$TEST_FILE" ]; then
      PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}"
      for EXT in ts tsx js jsx; do
        if [ -f "${PROJECT_ROOT}/src/__tests__/${BASENAME}.test.${EXT}" ]; then
          TEST_FILE="${PROJECT_ROOT}/src/__tests__/${BASENAME}.test.${EXT}"
          break
        fi
      done
    fi

    REASON=""

    if [ -z "$TEST_FILE" ]; then
      REASON="'${BASENAME}'에 대한 테스트 파일이 없습니다. 구현 코드를 작성하기 전에 테스트를 먼저 작성하세요.\n탐지 위치 (하나만 만들면 됩니다):\n  - 같은 폴더: ${BASENAME}.test.${SRC_EXT} 또는 ${BASENAME}.spec.${SRC_EXT}\n  - __tests__ 폴더: __tests__/${BASENAME}.test.${SRC_EXT} (같은 폴더 또는 상위 폴더)\n  - 프로젝트 루트: src/__tests__/${BASENAME}.test.${SRC_EXT}"
    # 빈 파일 / 주석뿐인 파일 / 전부 스킵된 파일로 우회하는 것을 막는다.
    # 주석과 문자열을 걷어낸 뒤 실제로 '실행되는' 케이스가 있는지 본다.
    elif ! strip_comments_and_strings "$TEST_FILE" | has_live_test_case; then
      REASON="'$(basename "$TEST_FILE")'에 실행되는 테스트 케이스가 없습니다. 빈 파일이나 skip/todo 로는 우회할 수 없습니다. 실제로 실행되는 it(...) 또는 test(...) 케이스를 먼저 작성하세요."
    fi

    if [ -n "$REASON" ]; then
      cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "TDD GUARD: ${REASON}"
  }
}
EOF
    fi
    ;;
esac

exit 0
