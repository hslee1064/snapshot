# Snapshot

GNOME Wayland(Ubuntu)용 스크린샷·화면 녹화 도구입니다. GNOME 기본 스크린샷 UI와 비슷하게 동작하며, 단축키와 CLI로도 쓸 수 있습니다.

## 기능

- **바로 영역 선택**: `snapshot`을 실행하면 곧바로 영역 선택 화면이 뜹니다. Esc를 누르면 캡처 창(대상·모드·지연 선택)이 열립니다.
- **대상**: 선택 영역 / 창(마우스를 올려 고르기) / 화면(포인터가 있는 모니터) / 전체(모든 모니터, CLI `all`)
- **스크린샷**: 캡처 후 미리보기 창이 뜹니다.
  - Ctrl+C 클립보드 복사, "저장"으로 위치 지정, 끌어다 놓기로 다른 앱에 붙여넣기, Esc는 저장 안 함
  - 기본 저장 위치는 `~/Downloads/` (PNG)
- **화면 녹화**: MP4 또는 GIF
  - 마우스 포인터 포함(기본), 시스템 오디오 선택
  - 녹화 중에는 선택 영역 밖이 어둡게 표시되고, 상단 표시줄에 🔴 경과 시간이 나옵니다. 클릭하면 중지
  - 종료 후 미리보기 창에서 재생·탐색하고 MP4/GIF로 저장
- **지연 촬영**: 0 / 3 / 5 / 10초 카운트다운

## 요구 사항

- Ubuntu 24.04 이상, GNOME on Wayland (GNOME Shell 45–50)
- xdg-desktop-portal-gnome, GStreamer + PipeWire, ffmpeg(GIF 변환), Python 3 + GTK 3 (PyGObject)

`./install.sh --deps`가 필요한 패키지를 apt로 설치합니다.

## 설치

```bash
git clone https://github.com/hslee1064/snapshot.git
cd snapshot
./install.sh --deps --shortcuts   # 의존성(apt) + 앱 + 단축키
```

| 명령 | 내용 |
|---|---|
| `./install.sh` | 앱만 설치 (`~/.local/bin/snapshot`, 런처, 아이콘, Shell 확장) |
| `./install.sh --deps` | apt 의존성도 설치 (sudo 필요) |
| `./install.sh --shortcuts` | GNOME 단축키 등록 |
| `./uninstall.sh` | 제거 |

창 선택용 GNOME Shell 확장(`snapshot@hyunsik1-lee`)도 함께 설치됩니다. **처음 설치한 뒤에는 한 번 로그아웃했다가 다시 로그인**해야 확장이 켜집니다. 확장이 없으면 창 대상은 GNOME 기본 창 선택 대화상자로 대신합니다.

`~/.local/bin`이 `PATH`에 있어야 합니다.

## 사용법

### 캡처 창 단축키

| 키 | 동작 |
|---|---|
| S / W / C | 대상: 영역 / 창 / 화면 |
| V | 스크린샷 ↔ 녹화 전환 |
| Enter | 캡처 / 녹화 시작 |
| Esc | 닫기 |

영역·창 선택 화면에서는 Esc나 오른쪽 클릭으로 취소하고 캡처 창으로 돌아갑니다.

### CLI

| 명령 | 동작 |
|---|---|
| `snapshot` | 영역 선택으로 시작 (Esc → 캡처 창) |
| `snapshot ui --video` | 녹화 모드로 캡처 창 열기 |
| `snapshot shot area\|window\|screen\|all [-d 초] [-o 파일.png] [--no-clipboard] [--no-preview]` | 스크린샷 |
| `snapshot record area\|window\|screen\|all [-a] [--no-pointer] [--fps 30] [-d 초] [-o 파일.mp4\|.gif] [--no-preview]` | 녹화 시작 |
| `snapshot stop` | 녹화 중지 |
| `snapshot toggle-record` | 녹화 중이면 중지, 아니면 녹화 모드로 캡처 창 열기 |

자세한 옵션은 `snapshot <명령> --help`로 확인하세요.

### 기본 단축키 (`--shortcuts`)

| 키 | 동작 |
|---|---|
| Super+Shift+S | Snapshot 실행 (영역 선택) |
| Ctrl+Shift+Print | 선택 영역 스크린샷 |
| Ctrl+Alt+Print | 창 스크린샷 |
| Ctrl+Shift+Alt+R | 녹화 시작/중지 |

Ubuntu 기본 Print 키와 겹치지 않게 잡았습니다. 바꾸려면 [shortcuts.sh](shortcuts.sh)의 `SHORTCUTS` 목록을 고친 뒤 `./shortcuts.sh install`을 다시 실행하세요.

## 동작 방식

- 스크린샷은 xdg-desktop-portal Screenshot API, 녹화는 ScreenCast 포털 + PipeWire 스트림을 GStreamer로 인코딩합니다.
- 선택 오버레이와 미리보기 창은 XWayland(GTK 3)로 띄웁니다.
- 녹화할 때 GNOME이 모니터별로 처음 한 번 공유할 화면을 묻고, 이후에는 기억합니다.
- 창 선택은 같이 들어 있는 작은 Shell 확장이 창 위치를 알려 주는 방식입니다. Wayland에서는 일반 앱이 창 목록을 볼 수 없기 때문입니다.
- 스크린샷에는 마우스 포인터가 들어가지 않습니다. 포인터 옵션은 녹화에만 적용됩니다.

## 파일 구성

| 파일 | 설명 |
|---|---|
| [snapshot.py](snapshot.py) | 앱 본체 (단일 파일) |
| [extension/](extension/) | 창 선택용 GNOME Shell 확장 |
| [install.sh](install.sh) / [uninstall.sh](uninstall.sh) | 설치 / 제거 |
| [shortcuts.sh](shortcuts.sh) | GNOME 단축키 등록·해제 |
| [snapshot.desktop](snapshot.desktop), [snapshot.svg](snapshot.svg) | 런처, 아이콘 |
