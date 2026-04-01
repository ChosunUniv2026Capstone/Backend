# Backend AGENTS

이 repo 는 LMS 도메인(강의, 과제, 시험, 공지, 사용자 흐름)과 관련 API 를 담당한다.

## 시작 전 필수
1. `git checkout main`
2. `git pull --ff-only origin main`
3. `git -C ../docs checkout main`
4. `git -C ../docs pull --ff-only origin main`

## 구현 전 반드시 확인할 문서
- 역할별 requirement
- `../docs/03-conventions/conv-service-boundary.md`
- `../docs/03-conventions/conv-api-response.md`
- `../docs/03-conventions/conv-auth-and-session.md`
- `../docs/04-architecture/service-map.md`
- 출석/시험 인증 영향이면:
  - `../docs/01-requirements/req-attendance-presence.md`
  - `../docs/01-requirements/req-device-auth.md`
  - `../docs/04-architecture/network-topology.md`
  - `../docs/02-decisions/adr-0004-attendance-authorization-flow.md`

## docs gap 규칙
다음이면 구현 중지:
- endpoint 는 필요한데 request/response/error 규약이 없음
- attendance 판정 책임 위치가 문서에 없음
- schema 영향이 있는데 data model / migration 기준이 없음

이 경우 `$spec-first-dev-guard` 절차를 따른다.

## Git 규칙
- 브랜치: `feat/<slug>` 등
- 커밋: `<type>(backend): <subject>`
- `main` 에서 직접 commit 하지 않는다. bootstrap 이 설치한 shared hook 이 이를 차단한다.
- 기능 시작은 `./start_feature.sh <slug>` 또는 `./start_feature.sh --worktree <slug>` 를 우선 사용한다.
- API / auth / attendance / schema 변경은 docs-first
- presence-service 역할을 backend 안에 복제하지 않는다.

## 권장 skill
- 개발 전 문서 검증: `$spec-first-dev-guard`
- Git 규약: `$git-governance`
