# LCK Discord Bot

LCK 경기 로스터, 실시간 경기상황, 최근 경기결과를 Discord 슬래시 명령어로 출력하는 봇입니다.

## 기능

- `/로스터`: 현재 세트 기준 팀, 포지션, 챔피언 픽, 선수명 표 출력
- `/경기상황`: KST 00:00 기준 오늘의 LCK 상태를 판단해 진행 중이면 현재 세트 정보, 시작 전이면 예정 시간, 모두 종료되었으면 `/경기결과` 대체 출력
- `/경기결과`: 최근 완료된 LCK 경기의 진행 세트수, 세트별 진행시간, 챔피언 픽, 선수별 K/D/A 출력

무료 LoL Esports API 경로에서는 명시적인 kill event feed가 아니라 프레임별 킬/데스 통계를 받습니다. 그래서 `/경기상황`의 킬러/피해자는 프레임 간 증가분으로 추론하며, 출력에도 `추론`으로 표시합니다. 동시에 여러 킬이 발생하면 한 줄에 묶어서 표시될 수 있습니다.

검증한 공개 API에서는 밴 챔피언, 총 딜량, 세트별 승리팀, 선수별 승패를 확정할 필드가 안정적으로 제공되지 않아 출력에서 제외했습니다. 정확한 밴 순서, 이벤트 단위 킬 로그, 확정 딜량까지 보장하려면 GRID/Riot Esports Data 또는 PandaScore/Bayes 같은 승인형 API가 필요합니다.

## 로컬 실행

```powershell
copy .env.example .env
```

`.env`에 Discord 봇 토큰을 넣습니다.

```env
DISCORD_TOKEN=...
DISCORD_GUILD_ID=
LCK_LOCALE=ko-KR
LCK_LEAGUE_ID=98767991310872058
LCK_API_KEY=
LCK_POLL_SECONDS=15
```

`DISCORD_GUILD_ID`를 넣으면 해당 서버에 명령어가 빠르게 동기화됩니다. 비워두면 글로벌 명령어로 등록되며 Discord 전파에 시간이 걸릴 수 있습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install .
python -m lck_bot.bot
```

## Docker 실행

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

## 무료 배포 권장안

### 1. Oracle Cloud Always Free VM

Discord 봇은 계속 켜져 있는 프로세스가 필요하므로 무료 배포는 Oracle Cloud Always Free VM이 가장 안정적입니다.

1. Oracle Cloud Free Tier 계정을 만듭니다.
2. Always Free 대상 Compute VM을 생성합니다.
3. VM에 Docker를 설치합니다.
4. 이 저장소를 VM에 올리고 `.env`를 설정합니다.
5. `docker compose up -d --build`로 실행합니다.

Oracle 공식 문서 기준 Always Free Compute 리소스는 기간 제한 없이 제공되지만, 지역별 용량 부족이 있을 수 있습니다.

### 2. Koyeb Free Instance

Koyeb은 무료 웹 서비스 인스턴스를 제공하지만 Free Instance는 HTTP 트래픽이 1시간 없으면 scale-to-zero 됩니다. Discord Gateway 연결을 유지해야 하는 봇에는 그대로 쓰기 어렵습니다.

### 3. Render/Fly.io

Render 무료 웹 서비스는 sleep 제한이 있고, background worker는 무료 상시 실행 용도로 적합하지 않습니다. Fly.io의 기존 무료 allowance는 legacy 조건이 붙어 있어 신규 무료 24/7 배포 대상으로 보기 어렵습니다.

## systemd로 직접 실행

Docker 대신 VM에서 venv로 직접 실행하려면 `deploy/lck-bot.service`를 참고해 `/opt/lck_bot`에 배치한 뒤 등록합니다.

```bash
sudo useradd --system --create-home lckbot
sudo cp deploy/lck-bot.service /etc/systemd/system/lck-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now lck-bot
sudo journalctl -u lck-bot -f
```

## 데이터 소스

- 일정/결과/이벤트 상세: LoL Esports persisted API
- 라이브 프레임: `https://feed.lolesports.com/livestats/v1/window/{gameId}`
- 정확한 공식 이벤트 단위 실시간 데이터가 필요하면 GRID/Riot Esports Data 또는 PandaScore/Bayes 같은 승인형 API 검토가 필요합니다.
