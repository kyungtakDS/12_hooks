# 프로젝트: 12_hooks — Claude Code Harness Kit

큰 작업을 여러 step으로 쪼개 각각 독립된 Claude 세션에서 실행하고,
가드 훅으로 품질을 강제하는 Claude Code 스타터 킷.

## 기술 스택
- Python 3.11 — 실행에는 **표준 라이브러리만** 쓴다 (런타임 외부 의존성 0개)
- Bash — 훅 스크립트
- pytest — 유일한 개발 의존성
- 프레임워크 없음. 빌드 단계 없음. Node/npm 사용하지 않음.

## 아키텍처 규칙

- CRITICAL: 훅 스크립트(`scripts/hooks/`, `.githooks/`)에 외부 의존성을 쓰지 말 것.
  jq 같은 도구가 없는 환경에서도 훅은 돌아야 한다. 셸 내장과 coreutils만 쓴다.

- CRITICAL: **훅은 fail-open, CI(PR 리뷰)는 fail-closed.**
  훅 오류로 로컬 작업을 멈추지 않는다 — 대신 건너뛴 이유를 반드시 출력한다.
  CI는 반대로, 리뷰를 완료하지 못하면 원인을 남기고 job을 실패시킨다.
  이 둘을 뒤바꾸지 마라. 이유: 로컬은 흐름이 끊기면 훅이 꺼지고, CI는 조용히 통과하면 의미가 없다.

- CRITICAL: `claude -p` 에 프롬프트를 **명령줄 인자로 넘기지 마라. stdin으로 넘겨라.**
  이유: Windows 명령줄 한도(32,767자)에 걸려 `Argument list too long`으로 죽는다.
  diff가 클수록 확실히 걸려서, 정작 검사가 가장 필요한 큰 변경에서 검사가 빠진다.

- 훅의 발동 조건을 **명령 접두사로 판정하지 마라.**
  이유: `git add . && git commit` 같은 복합 명령이 접두사 매칭을 그대로 빠져나간다.
  페이로드 전체를 검사한다. 과검사가 미검사보다 낫다.

- `scripts/pr_review.py` 는 표준 라이브러리만 쓴다. 이유: CI에 pip install 단계를 두지 않기 위해서다.

- 검증 로직은 순수 함수로 분리하고 부수효과(네트워크, `gh` 호출)는 얇게 감싼다. 이유: 테스트 가능하게.

- 셸 스크립트는 LF로 커밋한다(`.gitattributes`). 이유: CRLF면 shebang이 깨져 Unix에서 훅이 실행되지 않는다.

## 개발 프로세스

- CRITICAL: 새 기능 구현 시 반드시 테스트를 먼저 작성하고, 테스트가 통과하는 구현을 작성할 것 (TDD)
- CRITICAL: 기존 훅과 workflow를 **삭제하거나 덮어쓰지 마라.** 보완만 한다.
- 커밋 메시지는 conventional commits 형식을 따를 것 (feat:, fix:, docs:, refactor:)
- 컨텍스트가 200k 토큰을 초과하면 `/compact` 로 대화를 압축할 것

## 명령어

```bash
python -m pytest scripts/ -q     # 테스트 — 유일한 검증 명령
```

lint·build 단계는 **없다.** ruff·flake8·mypy·shellcheck 모두 설치되어 있지 않다.
AC(Acceptance Criteria)에 lint나 build 명령을 넣지 마라. 실행되지 않는다.

## 환경 (Windows)

PATH의 `python`/`python3`는 Windows Store alias stub이라 실행되지 않는다.
`C:\miniconda3\envs\flood_risk311\python.exe` 를 직접 호출할 것.
한글을 출력하는 스크립트에는 `PYTHONIOENCODING=utf-8` 을 붙인다.
