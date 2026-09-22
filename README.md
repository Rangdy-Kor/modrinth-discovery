# Modrinth Discovery

검토한 Modrinth 프로젝트를 SQLite에 기억하고, 아직 보지 않은 모드를 빠르게
Skip / Maybe / Reviewed로 분류한 뒤 선택한 Modrinth App 인스턴스에 일괄 적용하는
개인용 Windows 데스크톱 앱입니다.

## 실행

```powershell
uv sync
uv run modrinth-discovery
```

앱은 Modrinth App의 `%APPDATA%\ModrinthApp\app.db`를 읽기 전용으로 확인해
인스턴스를 자동 탐색합니다. 탐색되지 않으면 GUI에서 인스턴스 폴더를 직접
선택할 수 있습니다. 검토 기록은 `%LOCALAPPDATA%\modrinth-discovery\discovery.db`에
저장됩니다.

실행 흐름 진단 로그는 같은 디렉터리의 `diagnostic.log`에 기록됩니다. API 요청,
worker 시작/완료/오류, install 준비 단계와 UI busy 상태 전환을 포함하며 최대 1MB
단위로 순환 보관됩니다.

## 테스트

```powershell
uv run python -m unittest discover -v
```

Modrinth App 내부 DB는 읽기만 하며, 설치는 선택한 인스턴스의 `mods` 폴더에
검증된 JAR을 atomic replace 방식으로 기록합니다.

`Reviewed`는 인스턴스와 무관한 사용자 선택이고, `Installed`는 현재 인스턴스의
실제 JAR SHA-512와 Modrinth hash lookup으로 매번 계산됩니다. 제거한 파일은
`%LOCALAPPDATA%\modrinth-discovery\trash\<instance-id>`로 이동됩니다.

Reviewed 화면은 로컬 목록을 즉시 표시한 뒤 설치·호환성 상태를 행별로 갱신합니다.
변경되지 않은 JAR fingerprint/hash mapping과 24시간 동안 유효한 compatibility 결과는
SQLite cache에서 재사용되며, `Refresh`로 원격 mapping과 compatibility를 강제 갱신할
수 있습니다.
