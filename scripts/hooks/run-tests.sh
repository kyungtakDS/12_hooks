#!/bin/bash
# Test Runner Hook — Stop
# 응답이 끝날 때 테스트를 돌린다.
#
# 원래는 npm run lint/build/test 를 호출했으나 이 저장소는 Node 프로젝트가 아니다.
# package.json 이 없어 항상 즉시 exit 0 으로 빠지는 죽은 훅이었다.
#
# 쓸 수 있는 인터프리터를 찾지 못하면 통과시킨다 (fail open).
# 로컬 훅은 작업을 막지 않는다 — docs/ADR.md ADR-007.
#
# PATH 의 python 이 실행되지 않는 환경(Windows Store alias stub)이 있어서,
# 이름이 존재하는지가 아니라 pytest 가 실제로 도는지로 판정한다.
# 절대경로를 쓰려면 HARNESS_PYTHON 에 넣는다.

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

for PY in "$HARNESS_PYTHON" python3 python py; do
  [ -n "$PY" ] || continue
  "$PY" -m pytest --version >/dev/null 2>&1 || continue
  "$PY" -m pytest scripts/ -q
  exit $?
done

exit 0
