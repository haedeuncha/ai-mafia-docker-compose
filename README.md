# AI 마피아 Docker Compose 실행 패키지

FastAPI Backend, PostgreSQL, Redis, Streamlit 사용자 Frontend, FastMCP 런타임을
Docker Compose로 함께 기동하는 예시입니다. 게임 규칙과 상태 저장은 Backend가 담당하고,
MCP는 Backend 내부 API를 통해서만 컨텍스트와 도구를 제공합니다.

## 구성

| 서비스 | 역할 | 호스트 공개 여부 |
| --- | --- | --- |
| `postgresql` | `pgvector` 확장을 포함한 게임 데이터 원본 저장소 | 공개하지 않음 |
| `redis` | cache, lock, event stream 보조 계층 | 공개하지 않음 |
| `database_schema` | Backend migration 실행 후 종료 | 공개하지 않음 |
| `backend` | 공개 게임 API와 게임 진행 | `18000` |
| `frontend` | Streamlit 사용자 화면 | `18501` |
| `mcp_server1` | Backend가 사용하는 FastMCP runtime | `18100` |
| `mcp_server2` | 현재 단일 MCP URL 계약의 대기 인스턴스 | 공개하지 않음 |

## 시작하기

1. Docker Desktop을 실행합니다.
2. 예시 환경 파일을 실제 실행 파일로 복사합니다.

   ```powershell
   Copy-Item .env.example .env
   ```

3. 다음 명령으로 설정을 확인한 뒤 서비스를 시작합니다.

   ```powershell
   docker compose --env-file .env -f compose.yml config
   docker compose --env-file .env -f compose.yml up --build
   ```

4. 브라우저에서 다음 주소를 엽니다.

   - 사용자 화면: `http://127.0.0.1:18501`
   - Backend 상태 확인: `http://127.0.0.1:18000/health`
   - MCP endpoint: `http://127.0.0.1:18100/mcp`

초기 기동에서는 `database_schema`가 migration을 완료한 뒤 Backend가 시작됩니다.
PostgreSQL과 Redis 데이터는 Docker named volume에 보존됩니다.
PostgreSQL 이미지는 migration의 `vector` 확장을 제공하는 `pgvector/pgvector:pg16`입니다.

## Release 실행

```powershell
docker compose --env-file .env -f compose.yml -f compose.release.yml up --build -d
```

## 보안 및 사용 범위

- `.env.example`은 외부 호출이 없는 `LLM_PROVIDER=dummy`와 합성 개발 비밀번호만 포함합니다.
- 실제 API key, 팀 DB URL, 관리자 UUID는 `.env`에만 넣고 Git에 올리지 않습니다.
- 이 MVP의 UUID는 강한 인증 수단이 아니므로 개인 개발 또는 접근이 통제된 사설망에서만 사용하세요.
- Compose는 DB·Redis·보조 MCP 포트를 호스트에 공개하지 않습니다.

## 검증

```powershell
docker compose --env-file .env -f compose.yml -f compose.release.yml config --quiet
```
