# 12_hooks — Claude Code Harness Kit

큰 작업을 **여러 step으로 쪼개 각각 독립된 Claude 세션에서 순차 실행**하고, 가드 훅으로 품질을 강제하는 Claude Code 스타터 킷.

한 세션에 전부 밀어넣으면 컨텍스트가 길어질수록 지시를 잊고 품질이 떨어진다. 이 킷은 작업을 step 파일로 분해하고, 각 step을 새 세션에서 돌리되 **프로젝트 규칙(CLAUDE.md·docs)을 매번 프롬프트에 주입**해 그 문제를 줄인다.

---

## 구성 요소

| 구성 | 위치 | 역할 |
|---|---|---|
| Step 실행기 | `scripts/execute.py` | step을 순차 실행, 실패 시 자가 교정, 자동 커밋 |
| 슬래시 커맨드 | `.claude/commands/` | `/harness` (step 설계), `/review` (변경 리뷰) |
| 가드 훅 | `scripts/hooks/` | 위험 명령 차단, 테스트 없는 구현 차단 |
| GPT PR 리뷰 | `.github/workflows/pr-review.yml` | PR diff를 GPT API로 리뷰해 코멘트 |
| 문서 템플릿 | `docs/` | PRD · ARCHITECTURE · ADR (플레이스홀더 상태) |

---

## 빠른 시작

```bash
# 1. 프로젝트 규칙을 채운다 (플레이스홀더가 들어있다)
#    CLAUDE.md, docs/PRD.md, docs/ARCHITECTURE.md, docs/ADR.md

# 2. Claude Code에서 step 설계
/harness

# 3. 생성된 step들을 순차 실행
python3 scripts/execute.py <task-name>
python3 scripts/execute.py <task-name> --push   # 완료 후 push까지
```

---

## Harness 워크플로

`/harness`가 작업을 아래 구조로 분해한다.

```
phases/
├── index.json                 # 전체 task 현황
└── <task-name>/
    ├── index.json             # step별 status
    ├── step0.md               # step 지시서 (자기완결적)
    ├── step1.md
    └── ...
```

각 step 파일은 **독립된 세션에서 실행**되므로 "앞에서 논의한 대로" 같은 외부 참조를 쓰지 않고, 읽어야 할 파일 경로와 실행 가능한 Acceptance Criteria를 전부 담는다.

`execute.py`가 자동으로 처리하는 것:

- `feat-<task-name>` 브랜치 생성/checkout
- **가드레일 주입** — CLAUDE.md + `docs/*.md`를 매 step 프롬프트에 포함
- **컨텍스트 누적** — 완료된 step의 `summary`를 다음 step에 전달
- **자가 교정** — 실패 시 최대 3회 재시도하며 이전 에러를 프롬프트에 피드백
- **2단계 커밋** — 코드(`feat`)와 메타데이터(`chore`)를 분리
- 타임스탬프 자동 기록 (`started_at`, `completed_at`, `failed_at`, `blocked_at`)

### 상태와 복구

| status | 의미 | 복구 방법 |
|---|---|---|
| `pending` | 대기 | — |
| `completed` | AC 통과 | — |
| `error` | 3회 재시도 실패 | `status`를 `pending`으로 되돌리고 `error_message` 삭제 후 재실행 |
| `blocked` | 사람 개입 필요 (API 키·인증 등) | `blocked_reason` 해결 후 `pending`으로 되돌려 재실행 |

---

## 가드 훅

`.claude/settings.json`에 등록된 **Claude Code 훅**이다.

> ⚠️ 이것은 **git hook이 아니다.** `git commit` 시점이 아니라 **Claude의 도구 호출 시점**에 개입한다.
> 사람이 직접 `git commit`을 실행하면 이 훅들은 관여하지 않는다.

| 훅 | 이벤트 | 동작 |
|---|---|---|
| `bash-guard.sh` | `PreToolUse[Bash]` | `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE` 감지 시 차단 |
| `precommit-review.sh` | `PreToolUse[Bash]` | 커밋 직전 스테이징된 diff를 Claude가 리뷰하고, 심각한 문제가 있으면 차단 |
| `tdd-guard.sh` | `PreToolUse[Edit\|Write]` | 테스트 파일이 없으면 구현 코드 작성 차단 |
| npm 검증 | `Stop` | 응답 종료 시 `lint` → `build` → `test` 실행 (`package.json`이 있을 때만) |

### 커밋 전 리뷰 (`precommit-review.sh`)

Claude가 `git commit`을 실행하려 하면 `git diff --cached`를 `claude -p`에 넘겨 리뷰시키고,
버그·보안 결함·데이터 손실 위험이 발견되면 커밋을 차단한다. 스타일이나 취향은 지적하지 않는다.

설계상 주의한 점:

- **명령 접두사로 판정하지 않는다.** `git add . && git commit -m x` 같은 복합 명령이
  접두사 매칭을 그대로 빠져나가기 때문에, 페이로드 전체에서 `git commit`을 찾는다.
- **재귀 방지** — 리뷰용 중첩 세션에는 `CLAUDE_PRECOMMIT_REVIEW=1`이 설정되어 훅이 즉시 통과한다.
- **fail open** — `claude` 호출이 실패하거나 응답이 비면 커밋을 막지 않는다. 훅 오류로 작업이 멈추면 안 된다.
- 스테이징된 변경이 없으면 `claude`를 호출하지 않는다.

한계: **Claude가 도구로 실행하는 커밋만** 가로챈다. 사람이 터미널에서 직접 치는 `git commit`은
git hook이 아니므로 관여하지 않는다. 또한 걸리는 지점은 커밋이지 `git push`가 아니다.

### tdd-guard 적용 범위

`.ts` · `.tsx` · `.js` · `.jsx` **만** 검사한다. Python 등 다른 언어는 통과시킨다.

우회를 막기 위해 단순 존재 확인 이상을 한다:

- 주석과 문자열 리터럴을 걷어낸 뒤 검사 — 주석 속 `it(`으로 우회 불가
- `it.skip` / `it.todo` / `xit` 및 스킵된 `describe` 블록 내부는 케이스로 인정하지 않음
- 빈 테스트 파일로 통과 불가

탐지하는 테스트 파일 위치:

```
<같은 폴더>/<name>.test.ts   또는  .spec.ts
<같은 또는 상위 폴더>/__tests__/<name>.test.ts
src/__tests__/<name>.test.ts
```

---

## GPT PR 리뷰

PR이 열리거나 푸시되면 diff를 OpenAI Chat Completions API로 보내 리뷰 코멘트를 남긴다.

### 설정

```bash
gh secret set OPENAI_API_KEY          # 필수
gh variable set OPENAI_MODEL -b "..." # 선택, 기본 gpt-4o
gh variable set REVIEW_LANG -b "..."  # 선택, 기본 한국어
```

### 동작

- 트리거: `pull_request` (opened · synchronize · reopened) + `workflow_dispatch`(수동 재실행)
- **코멘트를 새로 달지 않고 기존 것을 갱신**해 PR이 도배되지 않음
- 같은 PR에 연속 푸시하면 이전 실행을 취소 (중복 리뷰·API 비용 방지)
- 포크에서 온 PR은 시크릿이 주입되지 않아 조용히 건너뜀
- lockfile · 이미지 · `*.min.js` 등을 제외하고 60,000자로 절단
- 리뷰할 코드 변경이 없으면 **API를 호출하지 않음**

프롬프트는 근거 없는 지적을 막도록 제약을 건다 — 재현 경로를 쓸 수 없으면 높은 심각도를 붙이지 못하고, "검토가 필요합니다" 류 표현은 금지 목록에 있으며, 지적은 최대 7개로 제한된다.

로컬에서도 그대로 돌릴 수 있다:

```bash
git diff main... > pr.diff
OPENAI_API_KEY=sk-... python3 scripts/pr_review.py --diff pr.diff --out review.md
```

---

## 디렉토리 구조

```
.
├── .claude/
│   ├── commands/          # /harness, /review 슬래시 커맨드
│   └── settings.json      # 훅 등록
├── .github/workflows/
│   └── pr-review.yml      # GPT PR 리뷰
├── docs/                  # PRD · ARCHITECTURE · ADR (채워 넣을 것)
├── scripts/
│   ├── execute.py         # step 실행기
│   ├── pr_review.py       # GPT 리뷰 생성기
│   ├── hooks/             # bash-guard, tdd-guard
│   └── test_*.py          # 단위 테스트
├── CLAUDE.md              # 프로젝트 규칙 (채워 넣을 것)
└── efficiency_report.html # 토큰 효율 분석 리포트 (생성물)
```

---

## 요구사항

- [Claude Code](https://claude.com/claude-code) — `claude` CLI가 PATH에 있어야 `execute.py`가 동작
- Python 3.9+ — 실행에는 표준 라이브러리만 쓴다 (런타임 외부 의존성 없음)
- `gh` CLI — PR 리뷰 워크플로에서 사용 (GitHub Actions 러너에는 기본 설치)

테스트 실행 (`pytest`가 필요하며, 이것이 유일한 개발 의존성이다):

```bash
pip install pytest
python3 -m pytest scripts/ -q
```
