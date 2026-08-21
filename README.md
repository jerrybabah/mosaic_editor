# Mosaic Editor

로컬 전용 영상 모자이크/블러 편집기. 브라우저 UI에서 영역을 드래그로 지정하고 시간 구간을 정하면, 로컬 `ffmpeg`으로 인코딩해 새 파일을 만듭니다.

- **단일 파일** — `mosaic_editor.py` 하나가 서버 + UI 전부입니다.
- **완전 오프라인** — CDN·외부 폰트·외부 네트워크 요청이 전혀 없습니다. HTML/CSS/JS는 Python 파일 안에 인라인으로 들어 있습니다.
- **원본 불변** — 입력 파일은 읽기만 하고 절대 수정하지 않습니다. 결과는 항상 새 파일로 저장됩니다.
- **로컬 바인드** — `127.0.0.1`에만 바인드합니다. 파일 업로드 없이 디스크에서 직접 읽습니다.

## 요구 사항

| 항목 | 내용 |
|---|---|
| Python | 3.10 이상 (pip 설치 불필요 — 표준 라이브러리만 사용) |
| 외부 의존성 | PATH 상의 `ffmpeg`, `ffprobe` 두 개뿐 |
| 브라우저 | `<video>`와 Pointer Events를 지원하는 최신 브라우저 |

ffmpeg이 없으면 시작 시 에러 메시지를 출력하고 종료합니다.

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Ubuntu / Debian
```

## 실행

```bash
python3 mosaic_editor.py /path/to/input.mp4
```

127.0.0.1의 사용 가능한 포트에 서버가 뜨고 기본 브라우저가 자동으로 열립니다. 종료는 터미널에서 `Ctrl+C`.

옵션:

| 옵션 | 설명 |
|---|---|
| `--port PORT` | 포트 고정 (기본값 0 = 자동 선택) |
| `--no-browser` | 브라우저 자동 열기 끄기 |

```bash
python3 mosaic_editor.py input.mp4 --port 8765 --no-browser
```

## 사용법

### 1. 영역 만들기

영상 위 **빈 곳을 드래그**하면 새 사각형이 생깁니다. 만들어진 사각형은 **내부를 드래그해 이동**, **8방향 핸들을 드래그해 크기 조절**을 할 수 있습니다. 선택된 사각형은 주황색으로 구분되고 핸들이 표시됩니다.

좌표는 화면 픽셀이 아니라 **영상 원본 픽셀 기준**으로 저장되므로, 브라우저 창 크기를 바꿔도 사각형은 영상의 같은 위치에 정확히 붙어 있습니다.

### 2. 시간 구간과 효과 지정

우측 패널에서 영역별로 설정합니다.

- **X / Y / W / H** — 원본 픽셀 단위 숫자 입력 (오버레이와 양방향 연동)
- **시작 / 끝** — 초 단위 입력, 또는 `현재 위치를 시작으로` / `현재 위치를 끝으로` 버튼으로 재생 위치를 그대로 반영
- **효과** — `모자이크` 또는 `블러`
- **강도** — 모자이크는 블록 크기 2~64px, 블러는 반경 1~50
- **삭제** — 해당 영역 제거

지정 구간(`시작`~`끝`) 밖의 프레임은 원본과 동일하게 유지됩니다. 영역은 최대 64개까지 지원합니다.

### 3. 확인하고 내보내기

하단 컨트롤:

- **근사 프리뷰** (체크박스) — 캔버스로 그린 대략적인 미리보기. 실시간이지만 실제 결과와 정확히 같지는 않습니다. 감을 잡는 용도.
- **정확한 프리뷰** — 현재 시각의 프레임에 **실제 ffmpeg 필터 체인을 적용한 PNG**를 모달로 보여줍니다. 이게 진짜 검증 수단입니다.
- **명령어 복사** — 실행될 ffmpeg 명령어 전체를 클립보드에 복사합니다. 터미널에서 직접 돌리거나 배치 스크립트로 옮길 때 사용하세요.
- **내보내기** — 인코딩을 시작하고 진행률 바를 갱신합니다. 완료되면 출력 경로가 표시되고, 실패하면 ffmpeg 에러 메시지가 모달로 뜹니다.

작업 상태는 `localStorage`에 저장되어 새로고침해도 유지됩니다.

### 단축키

| 키 | 동작 |
|---|---|
| `,` / `.` | 1프레임 뒤로 / 앞으로 (`currentTime ± 1/fps`) |
| `Space` | 재생 / 일시정지 |
| `Delete` / `Backspace` | 선택한 영역 삭제 |
| `Esc` | 모달 닫기 |

## 출력 파일

입력과 **같은 디렉터리**에 `<원본이름>_mosaic.mp4`로 저장됩니다. 같은 이름이 이미 있으면 `_mosaic_2`, `_mosaic_3`… 으로 번호가 붙습니다. 입력 파일을 덮어쓰는 일은 없습니다.

인코딩 설정은 `libx264 / preset medium / CRF 18 / yuv420p / +faststart`이며, 오디오는 재인코딩 없이 그대로 복사(`-c:a copy`)합니다. 오디오가 없는 영상도 정상 처리됩니다.

## 동작 방식

### 필터 체인

N개 영역에 대해 `split` → `crop` → 효과 → `overlay` 체인을 만듭니다.

```
[0:v]split=N+1[base][s0][s1]...;
[s0]crop=W0:H0:X0:Y0,<effect0>[fg0];
[base][fg0]overlay=X0:Y0:enable='between(t,S0,E0)'[v0];
...
최종 라벨 [vout]
```

- **모자이크** — `scale=cw:ch:flags=neighbor,scale=W:H:flags=neighbor` (`cw = max(1, round(W/블록크기))`)
- **블러** — `boxblur=luma_radius=R:luma_power=2` (R은 crop 크기의 절반 미만으로 클램프, 크로마 반경도 별도 클램프)

필터 그래프는 임시 파일에 쓴 뒤 파일로 전달합니다 — 체인이 길어질 때 인라인 인자의 이스케이프 문제를 피하기 위해서입니다. `-filter_complex_script`는 ffmpeg 9.0에서 제거되었기 때문에, 시작할 때 지원 여부를 감지해 구버전은 `-filter_complex_script`, 9.0 이상은 동등한 `-/filter_complex <파일>`을 사용합니다.

### 좌표 정규화 (서버에서 강제)

클라이언트가 무엇을 보내든 서버가 다시 정규화합니다.

- `X, Y, W, H`는 정수, **`W`와 `H`는 짝수** (yuv420p 인코딩 요구사항)
- `X ≥ 0`, `Y ≥ 0`, `X+W ≤ 영상너비`, `Y+H ≤ 영상높이`로 클램프
- `W ≥ 2`, `H ≥ 2`
- `0 ≤ 시작 < 끝 ≤ 영상길이`

해상도가 홀수인 영상은 마지막에 짝수로 crop해 인코딩 에러를 막습니다 (예: 639×361 → 638×360).

### 회전 메타데이터

ffmpeg은 디코딩할 때 회전을 자동 적용하고 `<video>`도 회전된 상태로 표시합니다. 그래서 기준 해상도는 **브라우저의 `videoWidth`/`videoHeight`**로 삼고, 서버도 ffprobe의 rotation 값(`side_data_list` 또는 `tags.rotate`)을 반영한 보정 해상도를 사용합니다. 세로로 촬영한 아이폰 영상(1920×1080 + rotation 90 → 1080×1920)도 그대로 동작합니다.

## API

직접 호출하거나 스크립트로 자동화할 때 참고하세요.

| 엔드포인트 | 설명 |
|---|---|
| `GET /` | 에디터 HTML |
| `GET /video` | 원본 스트리밍. HTTP Range(206 Partial Content) 지원 — 타임라인 시킹에 필요 |
| `GET /meta` | `width`, `height`, `display_width`, `display_height`, `duration`, `fps`, `avg_frame_rate`, `rotation`, `has_audio` 등 |
| `POST /command` | `{regions}` → 실행될 ffmpeg 명령어 문자열 |
| `POST /preview` | `{t, regions}` → 해당 시각 프레임에 필터를 적용한 PNG |
| `POST /export` | `{regions}` → `{job_id, output_path}` (비동기 시작) |
| `GET /progress?job=<id>` | `{percent, elapsed, done, error, output_path}` |

`regions` 배열의 각 항목:

```json
{ "x": 100, "y": 80, "w": 200, "h": 150,
  "start": 1.0, "end": 3.5,
  "effect": "mosaic", "strength": 16 }
```

`effect`는 `"mosaic"` 또는 `"blur"`. 잘못된 값은 400과 함께 한국어 에러 메시지를 반환합니다.

```bash
# 예: 2초 지점 프리뷰를 PNG로 저장
curl -s -X POST http://127.0.0.1:8765/preview \
  -H 'Content-Type: application/json' \
  -d '{"t":2.0,"regions":[{"x":100,"y":80,"w":200,"h":150,"start":0,"end":6,"effect":"mosaic","strength":16}]}' \
  -o preview.png
```

## 참고

- 진행률은 stderr 파싱이 아니라 `-progress pipe:1`의 `out_time_ms`(실제 단위는 마이크로초)를 영상 길이로 나눠 계산합니다.
- 모든 ffmpeg 호출은 `shell=False` + 리스트 인자로 실행하므로 경로에 공백이나 한글이 있어도 안전합니다.
- 내보내기 도중 `Ctrl+C`로 종료하면 진행 중이던 ffmpeg을 정리하고 잘린 부분 출력물과 임시 파일을 지웁니다.
