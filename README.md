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

## 시작하기

### 준비물

- Windows PowerShell
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) 설치 및 실행
- Git 설치

Docker Desktop 창 왼쪽 아래에 초록색 엔진 상태가 보이면 Docker가 실행된 것입니다. Git 설치 여부는 PowerShell에서 아래 명령으로 확인할 수 있습니다.

```powershell
git --version
```

### GitHub에서 받아서 실행하기

PowerShell을 열고, 프로젝트를 둘 폴더에서 다음 명령을 한 줄씩 실행합니다. 최신 Docker 수정본이 들어 있는 브랜치를 직접 받는 명령입니다.

```powershell
git clone -b fix/pgvector-migration https://github.com/haedeuncha/ai-mafia-docker-compose.git
cd ai-mafia-docker-compose
```

- `git clone`: GitHub 저장소를 내 컴퓨터에 복사합니다.
- `-b fix/pgvector-migration`: 현재 수정된 Docker 실행 구성이 있는 브랜치를 선택합니다.
- `cd ai-mafia-docker-compose`: 내려받은 프로젝트 폴더로 이동합니다.

프로젝트 폴더로 이동한 뒤 예시 환경 파일을 실제 실행 파일로 복사합니다.

   ```powershell
   Copy-Item .env.example .env
   ```

다음 명령으로 설정을 확인한 뒤 서비스를 시작합니다.

   ```powershell
   docker compose --env-file .env -f compose.yml config
   docker compose --env-file .env -f compose.yml up --build -d
   ```

시작이 끝나면 브라우저에서 다음 주소를 엽니다.

   - 사용자 화면: `http://127.0.0.1:18501`
   - Backend 상태 확인: `http://127.0.0.1:18000/health`
   - MCP endpoint: `http://127.0.0.1:18100/mcp`

초기 기동에서는 `database_schema`가 migration을 완료한 뒤 Backend가 시작됩니다.
PostgreSQL과 Redis 데이터는 Docker named volume에 보존됩니다.
PostgreSQL 이미지는 migration의 `vector` 확장을 제공하는 `pgvector/pgvector:pg16`입니다.

## 초보자용 실행 명령어 설명

### 이미 받은 프로젝트를 처음 실행할 때

프로젝트 폴더에서 아래 명령을 순서대로 입력합니다.

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f compose.yml up --build -d
```

- `Copy-Item .env.example .env`: 실행에 필요한 환경설정 파일의 사본을 만듭니다. 처음 한 번만 하면 됩니다.
- `docker compose`: 여러 컨테이너를 한 묶음으로 관리하는 Docker 명령입니다.
- `up`: 컨테이너를 생성하고 시작합니다.
- `--build`: 코드나 Dockerfile이 바뀌었을 때 실행 이미지를 새로 만듭니다.
- `-d`: 터미널을 계속 점유하지 않고, 컨테이너를 뒤에서 실행합니다.

실행이 끝나면 브라우저에서 `http://127.0.0.1:18501`을 열면 됩니다.

### 실행 상태 확인하기

```powershell
docker compose --env-file .env -f compose.yml ps
```

`Up` 또는 `healthy`가 보이면 실행 중입니다. `postgresql`, `redis`, `backend`, `frontend`, `mcp_server1`이 실행 상태여야 합니다.

`database_schema`가 `Exited (0)`으로 보이는 것은 정상입니다. 이 컨테이너는 DB 테이블을 만든 뒤 자동으로 종료하는 1회성 작업입니다. 데이터베이스 서버는 `postgresql`이며 `Up (healthy)`로 표시되어야 합니다.

### 문제가 생겼을 때 로그 보기

```powershell
docker compose --env-file .env -f compose.yml logs backend
```

위 명령의 `backend` 자리에 `frontend`, `postgresql`, `mcp_server1`을 넣으면 해당 서비스의 오류 메시지를 볼 수 있습니다. 계속 새 로그를 보고 싶다면 명령 끝에 `-f`를 붙입니다.

```powershell
docker compose --env-file .env -f compose.yml logs -f frontend
```

멈추려면 `Ctrl + C`를 누릅니다.

### 종료하고 다시 시작하기

```powershell
docker compose --env-file .env -f compose.yml down
docker compose --env-file .env -f compose.yml up -d
```

- `down`: 실행 중인 컨테이너를 중지하고 제거합니다.
- `up -d`: 이미 만든 이미지를 사용해 다시 실행합니다. 코드 변경이 없으면 `--build`는 필요 없습니다.

`down`만으로 PostgreSQL 데이터가 지워지지 않습니다. 게임 데이터는 Docker volume에 남아 있습니다.

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
