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
| GPT PR 리뷰 | `.github/workflows/pr-review.yml` | PR diff를 GPT API로 리뷰해 Risk Score와 함께 코멘트 |
| 문서 템플릿 | `docs/` | PRD · ARCHITECTURE · ADR (플레이스홀더 상태) |

---

## 빠른 시작

```bash
# 0. git 훅 활성화 (클론 후 1회 — 하지 않으면 pre-push 리뷰가 동작하지 않는다)
git config core.hooksPath .githooks

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
| `run-tests.sh` | `Stop` | 응답 종료 시 `pytest` 실행. 쓸 수 있는 인터프리터가 없으면 통과 |

`run-tests.sh`는 `python3` → `python` → `py` 순으로 시도하되, **이름이 있는지가 아니라
`pytest`가 실제로 도는지로 판정한다.** Windows의 `python`은 실행되지 않는 Store alias stub이라
이름만 보면 오판한다. 절대경로를 쓰려면 `HARNESS_PYTHON` 환경변수에 넣는다.

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
git hook이 아니므로 관여하지 않는다. 그 사각지대는 아래 `pre-push` 훅이 메운다.

---

## git pre-push 훅

`.githooks/pre-push`는 **진짜 git 훅**이다. git이 직접 호출하므로 **누가 만든 커밋이든**,
사람이 터미널에서 직접 만든 것까지 전부 검사한다.

### 설치 (클론 후 1회)

git은 `.git/hooks/`를 추적하지 않으므로, 추적되는 `.githooks/`를 쓰도록 한 번 지정해야 한다.

```bash
git config core.hooksPath .githooks
```

이 설정을 하지 않으면 훅은 그냥 동작하지 않는다 (조용히 건너뛴다).

### 동작

push 하려는 커밋 범위의 diff를 Claude가 리뷰하고, 심각한 문제가 있으면 push를 중단한다.

| 상황 | 동작 |
|---|---|
| 시크릿·버그 발견 | **push 차단** (exit 1), 파일:라인과 이유 출력 |
| 문제 없음 | 통과 |
| 브랜치 삭제 push | 검사 없이 통과 |
| 올릴 변경 없음 | 검사 없이 통과 |
| `claude` 없음 / 리뷰 실패 / 타임아웃 | **통과** (fail open) — 단, 이유를 반드시 출력한다 |
| diff가 상한 초과 | 뒷부분을 자르고 **잘렸다고 경고** 후 검사 |

새 브랜치를 push할 때는 원격에 비교 대상이 없으므로 기본 브랜치와의 분기점을 기준으로 삼는다.

### 설정 (환경변수)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `PREPUSH_REVIEW_MODEL` | `claude-haiku-4-5-20251001` | 리뷰 모델 |
| `PREPUSH_REVIEW_TIMEOUT` | `240` | 초 단위 타임아웃 |
| `PREPUSH_REVIEW_MAX_CHARS` | `60000` | diff 상한 (바이트) |

커밋 전 훅도 같은 방식으로 `PRECOMMIT_REVIEW_MODEL` · `PRECOMMIT_REVIEW_TIMEOUT` ·
`PRECOMMIT_REVIEW_MAX_CHARS`(기본 150초)를 받는다.

**왜 haiku가 기본값인가** — 같은 44KB diff로 실측한 결과다.

| 모델 | 결과 |
|---|---|
| `claude-sonnet-5` | 180초 안에 못 끝냄 → 검사가 통째로 생략됨 |
| `claude-haiku-4-5` | **78초에 완료.** 30KB diff 끝에 묻어둔 시크릿도 잡아냄 |

push를 몇 분씩 붙잡아두는 리뷰는 결국 `--no-verify`로 꺼지게 된다.
더 꼼꼼한 리뷰를 원하면 `PREPUSH_REVIEW_MODEL`을 바꾸고 타임아웃도 함께 올릴 것.

### 프롬프트는 stdin으로 넘긴다

인자로 넘기면 Windows 명령줄 한도(32,767자)에 걸려 `Argument list too long`(exit 126)으로 죽는다.
diff가 클수록 확실히 걸리므로, **정작 검사가 가장 필요한 큰 변경에서 검사가 빠지는** 문제였다.

### 우회

```bash
git push --no-verify
```

### 두 리뷰 훅의 차이

| | `precommit-review.sh` | `.githooks/pre-push` |
|---|---|---|
| 종류 | Claude Code 훅 | git 훅 |
| 시점 | 커밋 직전 | push 직전 |
| 검사 대상 | **Claude가 실행한** 커밋만 | **모든** 커밋 (사람이 만든 것 포함) |
| 범위 | 스테이징된 diff | push할 커밋 범위 전체 |
| 우회 | — | `--no-verify` |

중복이 아니라 서로의 사각지대를 메운다. 커밋 훅은 문제를 일찍 잡고,
push 훅은 사람이 만든 커밋까지 포함해 원격에 나가기 전 마지막으로 거른다.

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

PR이 열리거나 업데이트되면 diff를 OpenAI Chat Completions API로 보내
**Risk Score(0~100)와 항목별 근거**를 PR Conversation에 Markdown 댓글로 남긴다.

### 실행 조건

| 항목 | 값 |
|---|---|
| 트리거 | `pull_request` — `opened` · `synchronize` · `reopened` |
| 수동 실행 | `workflow_dispatch` (PR 번호 입력) |
| 건너뛰는 경우 | 포크에서 온 PR (시크릿이 주입되지 않아 반드시 실패하므로) |
| 동시 실행 | 같은 PR에 연속 푸시하면 이전 실행을 취소 |
| 권한 | `contents: read`, `pull-requests: write` |

### 필요한 GitHub Secret / Variable

```bash
gh secret set OPENAI_API_KEY          # 필수 — 없으면 workflow가 실패한다
gh variable set OPENAI_MODEL -b "..." # 선택, 기본 gpt-4o
gh variable set REVIEW_LANG -b "..."  # 선택, 기본 한국어
```

`GITHUB_TOKEN`은 Actions가 자동으로 넣어주므로 따로 등록하지 않는다.

### Risk Score 계산 기준

항목별로 점수를 매기고 **합계**를 총점으로 쓴다. 만점의 합은 100이다.
기준 전문은 [`scripts/risk-rubric.md`](scripts/risk-rubric.md)에 있고, 프롬프트에 그대로 주입된다.

| 평가 항목 | 만점 | 무엇을 보는가 |
|---|---:|---|
| 보안 | 30 | API 키·시크릿 노출, 명령/SQL 주입, 경로 조작, 권한·인증 우회, 안전하지 않은 역직렬화 |
| 변경 범위 및 복잡도 | 20 | 과도한 변경, 한 커밋에 섞인 여러 책임, 복잡도 증가, 검토하기 어려운 대규모 변경 |
| Breaking change | 20 | 기존 API·함수 시그니처·설정 키·파일 구조·CLI 사용법의 비호환 변경 |
| 테스트 및 검증 | 15 | 테스트 누락, 깨질 가능성, 경계조건 미검증, 빈 테스트나 skip으로 우회 |
| 마이그레이션 및 운영 영향 | 15 | 배포·데이터·설정·의존성 변경, 롤백 불가, 마이그레이션 누락 |

점수와 근거가 어긋나지 않도록 도구가 다음을 강제한다:

- **근거(`evidence`)가 비어 있으면 그 항목은 0점으로 내린다.** 근거 없이 높은 점수를 줄 수 없다.
- 항목 점수가 만점을 넘거나 음수면 잘라낸다.
- 점수가 숫자가 아니면 0점으로 본다.
- 누락된 항목은 0점으로 채운다.
- **총점과 등급은 모델이 아니라 도구가 계산한다.** 모델의 산수를 믿지 않는다.

### 등급 구간

| 총점 | 등급 |
|---|---|
| 0–20 | 🟢 Low |
| 21–40 | 🟡 Moderate |
| 41–60 | 🟠 High |
| 61–80 | 🔴 Very High |
| 81–100 | 🚨 Critical |

### PR 댓글 예시

```markdown
## 🤖 GPT PR 리뷰

**Risk Score: 12 / 100 — 🟢 Low**

| 평가 항목 | 점수 | 만점 | 주요 근거 |
|---|---:|---:|---|
| 보안 | 0 | 30 | 문제 없음 |
| 변경 범위 및 복잡도 | 2 | 20 | README와 workflow 일부 변경 |
| Breaking change | 0 | 20 | 문제 없음 |
| 테스트 및 검증 | 10 | 15 | 신규 동작 테스트 일부 부족 |
| 마이그레이션 및 운영 영향 | 0 | 15 | 문제 없음 |

### 주요 검토 결과

**발견된 문제**
- scripts/pr_review.py:120 — 응답이 배열이면 처리되지 않음

**권장 수정 사항**
- 파싱 결과가 dict인지 확인 후 사용

**잘된 점**
- 경계값 테스트가 충실함

### 권장 처리

- 수정 후 merge
```

### 댓글 중복 방지

본문 첫 줄에 `<!-- gpt-pr-review -->` 마커를 심는다.
재실행하면 **마커가 든 기존 댓글을 찾아 갱신**하고, 없을 때만 새로 단다.
마커가 든 댓글이 여러 개면 가장 오래된 것을 갱신해 항상 같은 댓글로 모인다.

### 실패 정책 — fail-closed

PR 리뷰는 **조용히 통과시키지 않는다.** 아래 경우 PR 댓글과 Actions 로그에 원인을 남기고
workflow를 실패 처리한다(job이 붉게 남는다).

| 상황 | 동작 |
|---|---|
| `OPENAI_API_KEY` 없음 | 댓글에 등록 방법 안내 + 종료 코드 1 |
| API 호출 실패 · HTTP 오류 · 타임아웃 | 댓글에 상태 코드와 응답 본문 + 종료 코드 1 |
| 응답이 JSON이 아님 · 파싱 실패 | 댓글에 원본 응답 일부 첨부 + 종료 코드 1 |
| 리뷰할 코드 변경 없음 | Risk 0으로 정상 종료 (API 미호출) |

> 훅(`precommit-review.sh`, `.githooks/pre-push`)은 반대로 **fail-open**이다.
> 로컬 작업을 훅 오류로 멈추게 하지 않으려는 의도적 차이다.

### 기타 동작

- lockfile · 이미지 · `*.min.js` 등을 제외하고 60,000자로 절단
- 리뷰할 코드 변경이 없으면 **API를 호출하지 않음**
- 프롬프트가 근거 없는 지적을 막는다 — "검토가 필요합니다" 류 표현은 금지 목록, 지적은 최대 7개

### 로컬 테스트

API를 호출하지 않는 단위 테스트:

```bash
python3 -m pytest scripts/test_pr_review.py -q
```

실제 API로 리뷰만 생성 (댓글은 달지 않음):

```bash
git diff main... > pr.diff
OPENAI_API_KEY=sk-... python3 scripts/pr_review.py --diff pr.diff --out review.md
cat review.md
```

PR에 댓글까지 남기려면 `--repo`와 `--pr`을 준다 (`gh` 로그인 필요):

```bash
OPENAI_API_KEY=sk-... python3 scripts/pr_review.py \
  --diff pr.diff --out review.md --repo owner/repo --pr 123
```

---

## 디렉토리 구조

```
.
├── .claude/
│   ├── commands/          # /harness, /review 슬래시 커맨드
│   └── settings.json      # 훅 등록
├── .githooks/
│   └── pre-push           # git 훅 — push 전 Claude 리뷰 (core.hooksPath 필요)
├── .github/workflows/
│   └── pr-review.yml      # GPT PR 리뷰
├── docs/                  # PRD · ARCHITECTURE · ADR (채워 넣을 것)
├── scripts/
│   ├── execute.py         # step 실행기
│   ├── pr_review.py       # GPT 리뷰 생성기 + PR 댓글 게시
│   ├── risk-rubric.md     # Risk Score 채점 기준 (프롬프트에 주입)
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
