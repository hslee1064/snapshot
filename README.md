# Snapshot

Ubuntu 24.04 기본 스크린샷 도구와 같은 기능을 하는 캡처 도구입니다 (X11 세션 전용).

- **대상**: 선택 영역 / 창 / 화면(포인터가 있는 모니터) / 전체(모든 모니터, CLI `all`)
- **스크린샷**: PNG로 `~/Pictures/Screenshots/`에 저장, 클립보드 복사, 캡처 후 미리보기 창에서 "저장"(위치 지정) 또는 Esc(저장 안 함), 클립보드 자동 복사, 끌어다 놓기
- **화면 녹화**: 녹화 중 선택 영역 밖은 어둡게 표시, 종료 후 미리보기 창에서 재생·일시정지·탐색 막대, MP4/GIF 선택 후 "저장"(위치 지정),
  녹화 중에는 상단 표시줄에 🔴 경과 시간이 표시되고, 클릭하거나 Esc를 누르면 중지

## 설치

```bash
./install.sh --deps --shortcuts   # 의존성(apt) + 앱 + 단축키
./install.sh                      # 앱만
./uninstall.sh                    # 제거
```

## 사용

| 명령 | 동작 |
|---|---|
| `snapshot` | 캡처 창 열기 (S/C/W 대상, V 모드 전환, Enter 캡처) |
| `snapshot shot area\|window\|screen\|all [-p] [-d 초] [-o 파일] [--no-preview]` | 스크린샷 |
| `snapshot record area\|window\|screen\|all [-a] [--no-pointer] [--fps 30] [-o 파일.mp4\|.gif] [--no-preview]` | 녹화 시작 |
| `snapshot stop` | 녹화 중지 |
| `snapshot toggle-record` | 녹화 중이면 중지, 아니면 녹화 모드로 창 열기 |

선택 화면에서 Esc나 오른쪽 클릭으로 취소합니다.

## 기본 단축키 (`--shortcuts`)

| 키 | 동작 |
|---|---|
| Super+Shift+S | 캡처 창 |
| Ctrl+Shift+Print | 선택 영역 스크린샷 |
| Ctrl+Alt+Print | 창 스크린샷 |
| Ctrl+Shift+Alt+R | 녹화 시작/중지 |

Ubuntu 기본 Print 키와 겹치지 않게 잡았습니다. 바꾸려면 `shortcuts.sh`의 목록을 고친 뒤 다시 실행하세요.

## 제약

Wayland 세션에서는 ffmpeg x11grab을 쓸 수 없어 동작하지 않습니다.
로그인 화면에서 "Ubuntu on Xorg"를 선택하세요.
