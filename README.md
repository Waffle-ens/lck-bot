# LCK Discord Bot

LCK 경기 정보를 Discord 슬래시 명령어로 조회하는 봇입니다.

## 주요 기능

### `/로스터`

현재 세트 기준 팀, 포지션, 챔피언 픽, 선수명을 Embed로 표시합니다.

### `/경기상황`

KST 00:00 기준 오늘의 LCK 경기 상태를 판단해 알맞은 정보를 보여줍니다.

- 진행 중: 현재 세트 스코어, 골드, 바론, 드래곤, 챔피언 픽, 킬 타임라인
- 시작 전: 오늘 예정 경기와 시작 시간
- 다음 세트 준비 중: 곧 경기 시작 안내
- 모두 종료: `/경기요약` 내용을 대신 출력

### `/경기요약`

오늘 완료된 LCK 경기의 매치 단위 요약을 표시합니다.

- 매치 승패
- 세트 스코어
- 세트별 진행시간

### `/경기결과`

오늘 완료된 LCK 경기의 세트별 결과를 표시합니다. `/경기상황`에서 킬 타임라인을 제외한 구성입니다.

- 세트별 팀 킬 수
- 세트별 팀 골드와 골드차
- 바론 수
- 드래곤 수와 속성
- 챔피언 픽

## 쿨다운

- `/경기상황`: 유저당 30초, 서버당 10초
- `/로스터`: 유저당 2분, 서버당 30초
- `/경기결과`: 유저당 3분, 서버당 30초
- `/경기요약`: 유저당 3분, 서버당 30초

쿨다운 안내는 요청자에게만 보이는 Discord ephemeral Embed로 표시됩니다.

## 데이터 한계

무료 LoL Esports 공개 API에서는 다음 필드가 안정적으로 제공되지 않아 출력에서 제외했습니다.

- 밴 챔피언
- 총 딜량
- 유충
- 전령
- 확정 이벤트 단위 킬 로그
- 세트별 승리팀 확정 필드

킬 타임라인은 live frame의 선수별 K/D 변화 기반으로 추론합니다. 동시에 여러 킬이 발생하면 한 줄에 묶여 표시될 수 있습니다.

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

Discord 봇은 계속 켜져 있는 프로세스가 필요하므로 무료 배포는 Oracle Cloud Always Free VM을 우선 권장합니다.

```bash
docker compose up -d --build
```

Koyeb, Render 무료 플랜은 sleep 또는 scale-to-zero 정책이 있어 상시 실행 Discord Gateway 봇에는 제한이 있습니다.
