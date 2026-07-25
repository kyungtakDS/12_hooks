# 아키텍처

## 디렉토리 구조

```
.claude/
├── commands/          # 슬래시 커맨드 (/harness, /review)
└── settings.json      # Claude Code 훅 등록
.githooks/
└── pre-push           # git 훅 — core.hooksPath 설정 필요
.github/workflows/
├── pr-review.yml      # PR 리뷰 (Risk Score 댓글)
└── test.yml           # 테스트 실행
docs/                  # PRD · ARCHITECTURE · ADR
scripts/
├── execute.py         # Harness step 순차 실행기
├── pr_review.py       # 리뷰 생성 + PR 댓글 게시
├── risk-rubric.md     # Risk Score 채점 기준 (프롬프트에 주입)
├── test_*.py          # pytest 테스트
└── hooks/             # Claude Code 훅 스크립트
phases/                # harness 실행 시 생성
└── <task-name>/
    ├── index.json     # step별 status
    └── step{N}.md     # step 지시서
```

## 패턴

**훅은 두 계층이다.** 서로 못 잡는 것을 메운다.

| 계층 | 가로채는 것 | 검사 대상 |
|---|---|---|
| Claude Code 훅 | Claude의 **도구 호출** | Claude가 실행한 것만 |
| git 훅 | **git 명령** | 누가 만든 커밋이든 (사람 포함) |

**프롬프트는 코드에서 분리한다.** 채점 기준(`risk-rubric.md`)은 별도 파일로 두고 프롬프트에 주입한다.
기준을 고치는 일과 코드를 고치는 일이 섞이지 않게 하기 위해서다.

**순수 함수와 부수효과를 가른다.** 파싱·정규화·렌더링은 인자만 받는 순수 함수로 두고,
네트워크(`urllib`)와 `gh` 호출은 얇게 감싼다. 테스트가 네트워크 없이 돌아야 하기 때문이다.

**모든 자동 검사는 fail 방향이 정해져 있다.** 훅은 fail-open(통과시키되 이유 출력),
CI는 fail-closed(실패로 표시). CLAUDE.md의 CRITICAL 규칙이다.

## 데이터 흐름

**Harness 실행**
```
/harness → step{N}.md 생성
  → execute.py → CLAUDE.md + docs/*.md 를 가드레일로 주입
    → claude -p (독립 세션) → 코드 변경 + index.json status 갱신
      → 2단계 커밋(feat / chore) → 다음 step 에 summary 전달
```

**PR 리뷰 (CI)**
```
PR open/push → gh pr diff → filter_diff (lockfile·바이너리 제외)
  → risk-rubric.md 주입 → OpenAI chat completions → JSON
    → normalize (근거 없으면 0점) → 총점·등급 계산(도구가) → PR 댓글 갱신
```

**로컬 훅**
```
Claude가 git commit 시도 → precommit-review.sh → 스테이징 diff → claude -p → BLOCK/PASS
git push                  → .githooks/pre-push  → push 범위 diff → claude -p → BLOCK/PASS
```

## 상태 관리

harness 진행 상태는 **`phases/<task>/index.json` 하나**가 단일 소스다.

- `status`: `pending` → `completed` | `error` | `blocked`
- 타임스탬프(`started_at`·`completed_at`·`failed_at`·`blocked_at`)는 `execute.py`가 자동 기록
- `summary`는 각 step 세션이 기록하고, `execute.py`가 다음 step 프롬프트에 누적 전달

리뷰 결과는 상태로 저장하지 않는다. PR 댓글은 마커(`<!-- gpt-pr-review -->`)로 찾아 **갱신**하므로
매 실행이 멱등이다.
