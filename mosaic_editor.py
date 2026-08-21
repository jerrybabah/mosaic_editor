#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mosaic_editor.py — 로컬 전용 영상 모자이크/블러 편집기 (단일 파일, 표준 라이브러리만 사용)

사용법:
    python3 mosaic_editor.py /path/to/input.mp4
    python3 mosaic_editor.py input.mp4 --port 8765      # 포트 고정 (기본: 사용 가능한 포트 자동 선택)
    python3 mosaic_editor.py input.mp4 --no-browser     # 브라우저 자동 열기 끄기

동작:
    * 127.0.0.1 의 사용 가능한 포트에 HTTP 서버를 띄우고 기본 브라우저를 자동으로 연다.
    * 브라우저 UI에서 영상 위를 드래그해 영역을 만들고, 시간 구간과 효과(모자이크/블러),
      강도를 지정한 뒤 "내보내기"를 누르면 로컬 ffmpeg 로 인코딩한다.
    * 결과물은 입력과 같은 디렉터리에 <이름>_mosaic.mp4 (이미 있으면 _mosaic_2, _3 ...) 로
      저장된다. 원본 파일은 절대 수정하지 않는다.
    * Ctrl+C 로 종료.

요구 사항:
    * Python 3.10 이상 (pip 설치 불필요 — 표준 라이브러리만 사용)
    * PATH 에 ffmpeg / ffprobe (없으면 시작 시 에러 메시지를 출력하고 종료)

단축키:
    ,  /  .   : 1프레임 뒤로 / 앞으로
    Space     : 재생 / 일시정지
    Delete    : 선택한 영역 삭제
"""

import argparse
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 전역 상태
# ---------------------------------------------------------------------------

FFMPEG = None          # ffmpeg 실행 파일 경로
FFPROBE = None         # ffprobe 실행 파일 경로
INPUT_PATH = None      # 입력 영상 절대 경로
META = None            # probe_media() 결과
HAS_SCRIPT_OPT = True  # ffmpeg 가 -filter_complex_script 를 지원하는지 (9.0 에서 제거됨)

JOBS = {}              # job_id -> job dict
JOBS_LOCK = threading.Lock()
RESERVED_OUTPUTS = set()   # 동시 내보내기 시 출력 경로 충돌 방지

MAX_BODY = 10_000_000  # POST 본문 크기 상한 (bytes)

# ---------------------------------------------------------------------------
# ffprobe / 메타데이터
# ---------------------------------------------------------------------------


def _parse_rate(s):
    """'30000/1001' 같은 분수 문자열을 float fps 로 변환. 실패 시 0.0"""
    if not s:
        return 0.0
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path):
    """ffprobe 로 영상 메타데이터를 읽는다. 실패 시 메시지 출력 후 종료."""
    cmd = [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        sys.exit("오류: ffprobe 가 60초 안에 응답하지 않았습니다.")
    if proc.returncode != 0:
        sys.exit(f"오류: ffprobe 실행 실패\n{proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit("오류: ffprobe 출력(JSON)을 해석할 수 없습니다.")

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        sys.exit(f"오류: 비디오 스트림을 찾을 수 없습니다: {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        sys.exit("오류: 비디오 해상도를 읽을 수 없습니다.")

    avg_frame_rate = video.get("avg_frame_rate") or ""
    fps = _parse_rate(avg_frame_rate) or _parse_rate(video.get("r_frame_rate")) or 30.0

    duration = 0.0
    for src in (info.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(src)
            break
        except (TypeError, ValueError):
            continue
    if duration <= 0:
        # 마지막 수단: 프레임 수 / fps
        try:
            duration = float(video.get("nb_frames", 0)) / fps
        except (TypeError, ValueError, ZeroDivisionError):
            duration = 0.0
    if duration <= 0:
        sys.exit("오류: 영상 길이를 알 수 없습니다.")

    # 회전 메타데이터: side_data_list 의 rotation(Display Matrix) 우선, tags.rotate 폴백
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if isinstance(sd, dict) and "rotation" in sd:
            try:
                rotation = int(round(float(sd["rotation"])))
                break
            except (TypeError, ValueError):
                pass
    if rotation == 0:
        try:
            rotation = int(round(float((video.get("tags") or {}).get("rotate", 0))))
        except (TypeError, ValueError):
            rotation = 0
    rotation %= 360  # -90 -> 270

    # ffmpeg 은 디코딩 시 자동 회전하므로 필터/좌표 기준 해상도는 회전 보정된 값.
    # 브라우저의 videoWidth/videoHeight 도 같은 값을 보고한다.
    if rotation % 180 == 90:
        display_w, display_h = height, width
    else:
        display_w, display_h = width, height

    return {
        "width": width,
        "height": height,
        "display_width": display_w,
        "display_height": display_h,
        "duration": duration,
        "fps": fps,
        "avg_frame_rate": avg_frame_rate,
        "rotation": rotation,
        "has_audio": has_audio,
        "input_path": path,
        "input_name": os.path.basename(path),
    }


# ---------------------------------------------------------------------------
# 영역 정규화 / 필터 체인 생성
# ---------------------------------------------------------------------------


def normalize_region(raw, meta, idx):
    """클라이언트가 보낸 영역 하나를 서버 기준으로 강제 정규화한다.

    좌표는 회전 보정된 원본 픽셀 기준. X/Y/W/H 정수, W/H 는 짝수(yuv420p),
    경계 클램프, 0 <= S < E <= duration.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"영역 {idx + 1}: 형식이 잘못되었습니다.")
    label = f"영역 {idx + 1}"
    try:
        x = float(raw.get("x", 0))
        y = float(raw.get("y", 0))
        w = float(raw.get("w", 0))
        h = float(raw.get("h", 0))
        s = float(raw.get("start", 0))
        e = float(raw.get("end", meta["duration"]))
        strength = float(raw.get("strength", 16))
    except (TypeError, ValueError):
        raise ValueError(f"{label}: 숫자 필드가 잘못되었습니다.")
    for v in (x, y, w, h, s, e, strength):
        if v != v or v in (float("inf"), float("-inf")):  # NaN/inf 방어
            raise ValueError(f"{label}: 숫자 필드가 잘못되었습니다.")

    effect = raw.get("effect", "mosaic")
    if effect not in ("mosaic", "blur"):
        raise ValueError(f"{label}: 알 수 없는 효과 '{effect}' (mosaic|blur)")

    vw, vh = meta["display_width"], meta["display_height"]
    if vw < 2 or vh < 2:
        raise ValueError("영상 해상도가 너무 작습니다.")

    x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
    w = max(2, min(w, vw))
    h = max(2, min(h, vh))
    w -= w % 2          # 짝수 강제 (yuv420p)
    h -= h % 2
    w = max(2, w)
    h = max(2, h)
    x = max(0, min(x, vw - w))
    y = max(0, min(y, vh - h))

    dur = meta["duration"]
    s = max(0.0, min(s, dur))
    e = max(0.0, min(e, dur))
    if not s < e:
        raise ValueError(f"{label}: 시작({s:.3f}s)이 끝({e:.3f}s)보다 앞서야 합니다.")

    return {
        "x": x, "y": y, "w": w, "h": h,
        "start": s, "end": e,
        "effect": effect,
        "strength": int(round(strength)),
    }


def effect_filters(r):
    """정규화된 영역 하나에 대한 효과 필터 문자열."""
    w, h = r["w"], r["h"]
    if r["effect"] == "mosaic":
        block = max(1, min(r["strength"], 512))
        cw = max(1, round(w / block))
        ch = max(1, round(h / block))
        return f"scale={cw}:{ch}:flags=neighbor,scale={w}:{h}:flags=neighbor"
    # blur: boxblur 는 반경이 crop 크기 절반 이상이면 에러 (ffmpeg 9 는 radius < dim/2
    # 엄격 부등식) -> (min-1)//2 로 클램프. 최소 치수 2 인 영역은 radius 0 (no-op 허용).
    # yuv420p 는 크로마 평면이 절반 크기라 chroma_radius 도 별도로 클램프해야 한다
    # (지정하지 않으면 luma_radius 를 그대로 물려받아 작은 영역에서 에러).
    radius = max(0, min(r["strength"], (min(w, h) - 1) // 2))
    c_radius = max(0, min(radius, min(w, h) // 4 - 1))
    return (f"boxblur=luma_radius={radius}:luma_power=2"
            f":chroma_radius={c_radius}:chroma_power=2")


def build_filter_graph(regions, meta, with_enable=True, sep=";\n"):
    """스펙의 split/crop/overlay 체인 생성. 최종 라벨은 [vout].

    with_enable=False 는 프리뷰용(-ss 로 타임스탬프가 리셋되므로 enable 을 빼고,
    호출 측에서 해당 시각에 활성인 영역만 넘긴다).
    입력 해상도가 홀수이면 마지막에 짝수로 crop 해 yuv420p 인코딩 에러를 막는다.
    """
    vw, vh = meta["display_width"], meta["display_height"]
    ew, eh = vw - vw % 2, vh - vh % 2
    need_even = (ew != vw) or (eh != vh)

    n = len(regions)
    if n == 0:
        if need_even:
            return f"[0:v]crop={ew}:{eh}:0:0[vout]"
        return "[0:v]null[vout]"

    chains = []
    split_outs = "".join(f"[s{i}]" for i in range(n))
    chains.append(f"[0:v]split={n + 1}[base]{split_outs}")
    prev = "[base]"
    for i, r in enumerate(regions):
        chains.append(
            f"[s{i}]crop={r['w']}:{r['h']}:{r['x']}:{r['y']},{effect_filters(r)}[fg{i}]"
        )
        last = i == n - 1
        out = "[vout]" if (last and not need_even) else f"[v{i}]"
        enable = (
            f":enable='between(t,{r['start']:.3f},{r['end']:.3f})'" if with_enable else ""
        )
        chains.append(f"{prev}[fg{i}]overlay={r['x']}:{r['y']}{enable}{out}")
        prev = f"[v{i}]"
    if need_even:
        chains.append(f"[v{n - 1}]crop={ew}:{eh}:0:0[vout]")
    return sep.join(chains)


def normalize_regions(raw_list, meta):
    if not isinstance(raw_list, list):
        raise ValueError("regions 는 배열이어야 합니다.")
    if len(raw_list) > 64:
        raise ValueError("영역은 최대 64개까지 지원합니다.")
    return [normalize_region(r, meta, i) for i, r in enumerate(raw_list)]


# ---------------------------------------------------------------------------
# 출력 경로 / ffmpeg 실행
# ---------------------------------------------------------------------------


def unique_output_path(input_path):
    """<입력디렉터리>/<stem>_mosaic.mp4, 충돌 시 _mosaic_2, _3 ... 입력 덮어쓰기 금지."""
    d = os.path.dirname(input_path)
    stem = os.path.splitext(os.path.basename(input_path))[0]
    with JOBS_LOCK:
        n = 1
        while True:
            name = f"{stem}_mosaic.mp4" if n == 1 else f"{stem}_mosaic_{n}.mp4"
            cand = os.path.join(d, name)
            if cand not in RESERVED_OUTPUTS and not os.path.exists(cand) \
                    and os.path.abspath(cand) != os.path.abspath(input_path):
                RESERVED_OUTPUTS.add(cand)
                return cand
            n += 1


def filter_script_args(script_path):
    """필터 그래프를 파일로 전달하는 인자.

    체인이 길어지면 인라인 인자는 이스케이프 문제가 생기므로 항상 파일로 전달한다.
    ffmpeg 9.0 에서 -filter_complex_script 가 제거되고 -/filter_complex <파일>
    문법으로 대체되었으므로, 시작 시 감지한 결과(HAS_SCRIPT_OPT)에 따라 고른다.
    """
    if HAS_SCRIPT_OPT:
        return ["-filter_complex_script", script_path]
    return ["-/filter_complex", script_path]


def export_cmd(graph_source, out_path, use_script):
    """실행/표시용 ffmpeg 인자 리스트.

    use_script=True 이면 graph_source 는 필터 스크립트 파일 경로,
    False 이면 필터 그래프 문자열을 인라인 인자로 넣는다(복사용).
    """
    if use_script:
        filt = filter_script_args(graph_source)
    else:
        filt = ["-filter_complex", graph_source]
    return (
        [FFMPEG, "-y", "-i", INPUT_PATH]
        + filt
        + ["-map", "[vout]", "-map", "0:a?", "-c:a", "copy",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", "-loglevel", "error",
           out_path]
    )


def _run_export(job, graph, out_path):
    """내보내기 작업 스레드. -progress pipe:1 의 out_time_ms(µs)로 진행률 계산."""
    script_path = None
    proc = None
    try:
        fd, script_path = tempfile.mkstemp(prefix="mosaic_filter_", suffix=".txt")
        job["script_path"] = script_path   # Ctrl+C 종료 시 메인 스레드가 정리할 수 있도록
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(graph)

        cmd = export_cmd(script_path, out_path, use_script=True)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        job["proc"] = proc

        stderr_buf = []
        drain = threading.Thread(
            target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True
        )
        drain.start()

        dur = max(META["duration"], 0.001)
        for line in proc.stdout:
            line = line.strip()
            # 주의: ffmpeg 의 out_time_ms 는 이름과 달리 마이크로초 단위다.
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if us > 0:
                    job["percent"] = min(99.9, us / 1e6 / dur * 100.0)
            elif line == "progress=end":
                job["percent"] = max(job["percent"], 99.9)

        rc = proc.wait()
        drain.join(timeout=5)
        job["finished"] = time.time()
        if rc == 0:
            job["percent"] = 100.0
        else:
            err = (stderr_buf[0] if stderr_buf else "").strip()
            job["error"] = (err or f"ffmpeg 이 코드 {rc} 로 종료되었습니다.")[-4000:]
            # 실패한 부분 출력물은 지운다 (입력 파일은 경로가 다르므로 건드리지 않음)
            try:
                if os.path.exists(out_path) and \
                        os.path.abspath(out_path) != os.path.abspath(INPUT_PATH):
                    os.remove(out_path)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — 작업 스레드는 어떤 예외든 UI로 전달
        job["error"] = f"{type(e).__name__}: {e}"
        job["finished"] = job["finished"] or time.time()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        # 파이프 fd 회수 (wait() 는 부모 측 파이프를 닫지 않는다)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        job["proc"] = None
        job["done"] = True
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass
        # 실패로 출력 이름이 비었으면 다음 내보내기가 같은 이름을 다시 쓸 수 있게 예약 해제
        with JOBS_LOCK:
            RESERVED_OUTPUTS.discard(out_path)


def start_export(raw_regions):
    regions = normalize_regions(raw_regions, META)
    graph = build_filter_graph(regions, META, with_enable=True)
    out_path = unique_output_path(INPUT_PATH)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "percent": 0.0,
        "started": time.time(),
        "finished": None,
        "done": False,
        "error": None,
        "output_path": out_path,
        "proc": None,
        "script_path": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_export, args=(job, graph, out_path), daemon=True).start()
    return job_id, out_path


def render_preview_png(t, raw_regions):
    """t 시각 프레임 1장에 실제 필터 체인을 적용해 PNG 바이트를 반환."""
    regions = normalize_regions(raw_regions, META)
    try:
        t = float(t)
    except (TypeError, ValueError):
        raise ValueError("t 가 숫자가 아닙니다.")
    # 영역 선별은 요청 시각(t_sel) 기준 — 내보내기의 enable='between(t,S,E)' 와 일치.
    # -ss 만 duration-0.05 로 당겨(t_seek) 영상 끝에서도 프레임 1장을 확보한다
    # (-ss 는 t_seek 이상 첫 프레임에 착지하므로 표시 프레임은 요청 시각과 일치).
    t_sel = max(0.0, min(t, META["duration"]))
    t_seek = max(0.0, min(t, max(0.0, META["duration"] - 0.05)))

    # -ss 입력 시킹은 타임스탬프를 리셋하므로 enable='between(t,...)' 를 쓸 수 없다.
    # 대신 t 에 활성인 영역만 골라 enable 없이 그래프를 만든다.
    active = [r for r in regions if r["start"] <= t_sel <= r["end"]]
    graph = build_filter_graph(active, META, with_enable=False)

    script_path = png_path = None
    try:
        fd, script_path = tempfile.mkstemp(prefix="mosaic_prev_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(graph)
        fd, png_path = tempfile.mkstemp(prefix="mosaic_prev_", suffix=".png")
        os.close(fd)
        cmd = (
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t_seek:.3f}", "-i", INPUT_PATH]
            + filter_script_args(script_path)
            + ["-map", "[vout]", "-frames:v", "1", "-update", "1", png_path]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "ffmpeg 프리뷰 실패")
        if not os.path.getsize(png_path):
            raise RuntimeError("프레임을 추출하지 못했습니다 (영상 끝을 지난 시각일 수 있음).")
        with open(png_path, "rb") as f:
            return f.read()
    finally:
        for p in (script_path, png_path):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MosaicEditor/1.0"

    # ---- 유틸 ----

    def log_message(self, fmt, *args):  # 조용히
        pass

    def _send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        self._send_bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            # 본문을 읽지 않고 응답하면 keep-alive 연결에 잔여 바이트가 남아
            # 다음 요청 파싱이 어긋난다 -> 연결을 닫아 desync 를 막는다.
            self.close_connection = True
            raise ValueError("요청 본문이 비어 있습니다.")
        if length > MAX_BODY:
            self.close_connection = True
            raise ValueError("요청 본문이 너무 큽니다.")
        data = self.rfile.read(length)
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise ValueError("JSON 을 해석할 수 없습니다.")

    # ---- 라우팅 ----

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_bytes(PAGE_BYTES, "text/html; charset=utf-8")
            elif path == "/video":
                self._serve_video()
            elif path == "/meta":
                self._send_json({k: v for k, v in META.items()})
            elif path == "/progress":
                self._serve_progress()
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path == "/video":
            try:
                size = os.path.getsize(INPUT_PATH)
            except OSError:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", self._video_ctype())
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            self.send_response(200 if path == "/" else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        try:
            if path == "/command":
                regions = normalize_regions(body.get("regions", []), META)
                graph = build_filter_graph(regions, META, with_enable=True, sep=";")
                cmd = export_cmd(graph, unique_output_path_peek(), use_script=False)
                self._send_json({"command": shlex.join(cmd)})
            elif path == "/preview":
                png = render_preview_png(body.get("t", 0), body.get("regions", []))
                self._send_bytes(png, "image/png")
            elif path == "/export":
                job_id, out_path = start_export(body.get("regions", []))
                self._send_json({"job_id": job_id, "output_path": out_path})
            else:
                self._send_json({"error": "not found"}, 404)
        except ValueError as e:          # 잘못된 입력
            self._send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:           # noqa: BLE001 — ffmpeg 실패 등
            self._send_json({"error": f"{e}"}, 500)

    # ---- /video : HTTP Range 직접 구현 ----

    def _video_ctype(self):
        guessed = mimetypes.guess_type(INPUT_PATH)[0]
        return guessed if guessed and guessed.startswith("video/") else "video/mp4"

    def _serve_video(self):
        try:
            size = os.path.getsize(INPUT_PATH)
        except OSError:
            self._send_json({"error": "입력 파일을 읽을 수 없습니다."}, 404)
            return

        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", rng)
            if m and (m.group(1) or m.group(2)):
                s_str, e_str = m.group(1), m.group(2)
                if s_str == "":
                    # 접미 범위: 마지막 N 바이트
                    n = int(e_str)
                    if n == 0:
                        # 만족 불가능한 범위 -> 416
                        self._send_range_unsatisfiable(size)
                        return
                    start = max(0, size - n)
                    end = size - 1
                    status = 206
                elif e_str and int(e_str) < int(s_str):
                    # 역순 범위는 무효한 스펙 -> RFC 7233 에 따라 무시하고 200 전체 응답
                    start, end = 0, size - 1
                else:
                    start = int(s_str)
                    end = min(int(e_str), size - 1) if e_str else size - 1
                    if start >= size:
                        self._send_range_unsatisfiable(size)
                        return
                    status = 206
            # 형식이 이상하거나 다중 범위면 200 전체 응답으로 폴백

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", self._video_ctype())
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            with open(INPUT_PATH, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 브라우저는 Range 요청을 수시로 끊는다 — 정상 동작
            self.close_connection = True

    def _send_range_unsatisfiable(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- /progress ----

    def _serve_progress(self):
        q = parse_qs(urlparse(self.path).query)
        job_id = (q.get("job") or [""])[0]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self._send_json({"error": "알 수 없는 작업입니다."}, 404)
            return
        elapsed = (job["finished"] or time.time()) - job["started"]
        self._send_json({
            "percent": round(job["percent"], 2),
            "elapsed": round(elapsed, 2),
            "done": job["done"],
            "error": job["error"],
            "output_path": job["output_path"],
        })


def unique_output_path_peek():
    """/command 표시용: 예약 없이 다음 출력 경로만 계산 (진행 중 내보내기의 예약은 피함)."""
    d = os.path.dirname(INPUT_PATH)
    stem = os.path.splitext(os.path.basename(INPUT_PATH))[0]
    n = 1
    while True:
        name = f"{stem}_mosaic.mp4" if n == 1 else f"{stem}_mosaic_{n}.mp4"
        cand = os.path.join(d, name)
        with JOBS_LOCK:
            reserved = cand in RESERVED_OUTPUTS
        if not reserved and not os.path.exists(cand) \
                and os.path.abspath(cand) != os.path.abspath(INPUT_PATH):
            return cand
        n += 1


# ---------------------------------------------------------------------------
# 프론트엔드 (인라인 HTML/CSS/JS — 외부 네트워크 요청 없음)
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Mosaic Editor</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
       background:#14161a;color:#dfe4ea;font-size:13px;display:flex;flex-direction:column}
  button{font:inherit;background:#2b313b;color:#dfe4ea;border:1px solid #3a4250;border-radius:6px;
         padding:4px 10px;cursor:pointer}
  button:hover{background:#353d49}
  button:disabled{opacity:.45;cursor:default}
  input,select{font:inherit;background:#1d2128;color:#dfe4ea;border:1px solid #3a4250;border-radius:5px;padding:2px 5px}
  input[type=number]{width:64px}
  input[type=range]{padding:0}

  #topbar{padding:7px 12px;background:#1b1e24;border-bottom:1px solid #2a2e35;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:none}
  #topbar b{color:#8ec3ff}
  #main{flex:1;display:flex;min-height:0}
  #videoPane{flex:1;display:flex;flex-direction:column;min-width:0}
  #videoWrap{flex:1;position:relative;background:#000;min-height:0;overflow:hidden}
  #video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
  #overlay{position:absolute;touch-action:none;cursor:crosshair;display:none}

  .region{position:absolute;border:1.5px solid #57a9ff;background:rgba(87,169,255,.10);cursor:move}
  .region.selected{border-color:#ffb02e;background:rgba(255,176,46,.08);z-index:5}
  .region.inactive{border-style:dashed;opacity:.55}
  .region canvas.fx{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;display:none}
  .rlabel{position:absolute;left:0;top:0;background:rgba(20,22,26,.78);color:#cfe3ff;
          font-size:10px;padding:1px 5px;border-radius:0 0 5px 0;pointer-events:none;white-space:nowrap}
  .region.selected .rlabel{color:#ffd9a0}
  .handle{position:absolute;width:9px;height:9px;background:#fff;border:1px solid #333;border-radius:2px;z-index:6}
  .region:not(.selected) .handle{display:none}
  .h-nw{left:-5px;top:-5px;cursor:nwse-resize}
  .h-n{left:50%;top:-5px;margin-left:-4.5px;cursor:ns-resize}
  .h-ne{right:-5px;top:-5px;cursor:nesw-resize}
  .h-e{right:-5px;top:50%;margin-top:-4.5px;cursor:ew-resize}
  .h-se{right:-5px;bottom:-5px;cursor:nwse-resize}
  .h-s{left:50%;bottom:-5px;margin-left:-4.5px;cursor:ns-resize}
  .h-sw{left:-5px;bottom:-5px;cursor:nesw-resize}
  .h-w{left:-5px;top:50%;margin-top:-4.5px;cursor:ew-resize}

  #transport{display:flex;gap:8px;align-items:center;padding:8px 12px;background:#1b1e24;
             border-top:1px solid #2a2e35;flex:none}
  #seek{flex:1}
  #timeLabel{font-variant-numeric:tabular-nums;color:#9fb0c3;min-width:150px;text-align:right}

  #side{width:362px;background:#191c21;border-left:1px solid #2a2e35;display:flex;flex-direction:column;flex:none}
  #sideHead{padding:9px 12px;border-bottom:1px solid #2a2e35;color:#9fb0c3;flex:none}
  #regionList{flex:1;overflow-y:auto;padding:8px}
  #emptyHint{color:#6b7686;padding:14px 10px;line-height:1.6}
  .item{background:#20242b;border:1px solid #2b303a;border-radius:8px;padding:8px;margin-bottom:8px;cursor:pointer}
  .item.selected{border-color:#ffb02e}
  .itemHead{display:flex;gap:8px;align-items:center;margin-bottom:7px}
  .itemHead .badge{color:#8ec3ff;font-weight:600}
  .item.selected .itemHead .badge{color:#ffb02e}
  .itemHead .f-del{margin-left:auto;padding:2px 8px}
  .row{display:flex;gap:6px;align-items:center;margin-top:5px;flex-wrap:wrap}
  .row label{display:flex;align-items:center;gap:4px;color:#9fb0c3;font-size:12px}
  .row .grow{flex:1}
  .row .grow input[type=range]{flex:1}
  .row button{font-size:11px;padding:3px 7px}
  .f-strengthVal{color:#9fb0c3;font-size:11px;min-width:64px}
  .f-start,.f-end{width:84px}

  #bottom{display:flex;gap:10px;align-items:center;padding:10px 12px;background:#1b1e24;
          border-top:1px solid #2a2e35;flex:none}
  #bottom label{display:flex;align-items:center;gap:5px;color:#9fb0c3}
  #exportBtn{background:#2e5c39;border-color:#3f7a4d}
  #exportBtn:hover{background:#367044}
  #progressWrap{width:200px;height:10px;background:#2a2e35;border-radius:5px;overflow:hidden;flex:none}
  #progressBar{height:100%;width:0%;background:#4caf50;transition:width .3s}
  #progressBar.err{background:#d9534f}
  #statusText{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#9fb0c3}
  #statusText.err{color:#ff8a80}

  #modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;
         justify-content:center;z-index:50}
  #modal[hidden]{display:none}
  #modalCard{background:#20242b;border:1px solid #3a4250;border-radius:10px;max-width:90vw;
             max-height:88vh;display:flex;flex-direction:column;overflow:hidden}
  #modalHead{display:flex;align-items:center;gap:12px;padding:9px 14px;border-bottom:1px solid #2a2e35}
  #modalTitle{flex:1;color:#cfe3ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #modalBody{padding:12px;overflow:auto}
  .previewImg{max-width:84vw;max-height:72vh;display:block;background:#000}
  .cmdBox{width:72vw;max-width:900px;height:180px;background:#14161a;color:#c8e6c9;
          font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre-wrap;
          border:1px solid #3a4250;border-radius:6px;padding:8px}
  .errBox{max-width:72vw;max-height:60vh;overflow:auto;background:#14161a;color:#ff8a80;
          font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre-wrap;
          border:1px solid #5c2b2b;border-radius:6px;padding:10px;margin:0}
  #toast{position:fixed;left:50%;bottom:66px;transform:translateX(-50%);background:#2b313b;
         border:1px solid #3a4250;border-radius:8px;padding:8px 16px;z-index:60;color:#dfe4ea}
</style>
</head>
<body>
  <div id="topbar"><b>Mosaic Editor</b> <span id="fileInfo">로드 중…</span></div>
  <div id="main">
    <div id="videoPane">
      <div id="videoWrap">
        <video id="video" src="/video" preload="auto" playsinline></video>
        <div id="overlay"></div>
      </div>
      <div id="transport">
        <button id="playBtn" title="재생/일시정지 (Space)">▶</button>
        <button id="stepB" title="1프레임 뒤로 (,)">⏴</button>
        <button id="stepF" title="1프레임 앞으로 (.)">⏵</button>
        <input type="range" id="seek" min="0" max="0" step="0.001" value="0">
        <span id="timeLabel">0:00.000 / 0:00.000</span>
      </div>
    </div>
    <div id="side">
      <div id="sideHead">영역 목록 — 영상 위에서 드래그해 추가</div>
      <div id="regionList"></div>
      <div id="emptyHint">아직 영역이 없습니다.<br>
        영상 위 빈 곳을 드래그하면 새 사각형이 생기고,<br>
        사각형 내부 드래그로 이동 · 모서리 핸들로 크기 조절,<br>
        <b>Delete</b> 키로 삭제할 수 있습니다.</div>
    </div>
  </div>
  <div id="bottom">
    <label title="CSS/캔버스 근사 — 대략적인 감만 보는 용도"><input type="checkbox" id="approxToggle">근사 프리뷰</label>
    <button id="exactBtn" title="실제 ffmpeg 필터 체인을 현재 프레임에 적용해 확인">정확한 프리뷰</button>
    <button id="copyBtn">명령어 복사</button>
    <button id="exportBtn">내보내기</button>
    <div id="progressWrap"><div id="progressBar"></div></div>
    <span id="statusText"></span>
  </div>

  <div id="modal" hidden>
    <div id="modalCard">
      <div id="modalHead"><span id="modalTitle"></span><button id="modalClose">닫기 (Esc)</button></div>
      <div id="modalBody"></div>
    </div>
  </div>
  <div id="toast" hidden></div>

<script>
'use strict';
const $=s=>document.getElementById(s);
const video=$('video'), overlay=$('overlay'), wrap=$('videoWrap'), listEl=$('regionList');
const seekEl=$('seek'), timeLabel=$('timeLabel'), playBtn=$('playBtn');
const statusText=$('statusText'), progressBar=$('progressBar');

let meta=null, duration=0, fps=30;
let regions=[], selectedId=null, nextId=1;
let geo={scale:0, ox:0, oy:0};
let drag=null, approxOn=false, exporting=false, seekHeld=false;
const rectEls=new Map();
const osc=document.createElement('canvas'), osctx=osc.getContext('2d');

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const round3=v=>Math.round(v*1000)/1000;
const vidW=()=>video.videoWidth||(meta?meta.display_width:0);
const vidH=()=>video.videoHeight||(meta?meta.display_height:0);

/* ---------- 상태 저장/복원 (localStorage) ---------- */
function lsKey(){return 'mosaic_editor:'+(meta?meta.input_path:'');}
function stripRegion(r){return {x:r.x,y:r.y,w:r.w,h:r.h,start:r.start,end:r.end,effect:r.effect,strength:r.strength};}
function saveState(){
  try{ localStorage.setItem(lsKey(), JSON.stringify({regions:regions.map(stripRegion), approx:approxOn})); }
  catch(e){}
}
function loadState(){
  try{
    const raw=localStorage.getItem(lsKey()); if(!raw) return;
    const st=JSON.parse(raw);
    if(Array.isArray(st.regions)){
      regions=st.regions.map(r=>({
        id:nextId++,
        x:+r.x, y:+r.y, w:+r.w, h:+r.h,
        start:+r.start, end:+r.end,
        effect:r.effect==='blur'?'blur':'mosaic',
        strength:+r.strength
      }));
      regions.forEach(normalizeRegionGeom);
    }
    approxOn=!!st.approx; $('approxToggle').checked=approxOn;
  }catch(e){ console.warn('저장된 상태 복원 실패', e); }
}

/* ---------- 영역 정규화 (서버 규칙과 동일 — UI 표시 == 내보내기 결과) ---------- */
function normalizeRegionGeom(r){
  for(const k of ['x','y']) if(!Number.isFinite(r[k])) r[k]=0;
  for(const k of ['w','h']) if(!Number.isFinite(r[k])) r[k]=2;
  if(!Number.isFinite(r.start)) r.start=0;
  if(!Number.isFinite(r.end)) r.end=duration;
  if(!Number.isFinite(r.strength)) r.strength=r.effect==='blur'?10:16;
  r.x=Math.round(r.x); r.y=Math.round(r.y); r.w=Math.round(r.w); r.h=Math.round(r.h);
  const vw=vidW(), vh=vidH();
  if(vw>=2&&vh>=2){
    r.w=clamp(r.w,2,vw); r.h=clamp(r.h,2,vh);
    r.w-=r.w%2; r.h-=r.h%2;                 /* W/H 짝수 (yuv420p) */
    r.w=Math.max(2,r.w); r.h=Math.max(2,r.h);
    r.x=clamp(r.x,0,vw-r.w); r.y=clamp(r.y,0,vh-r.h);
  }
  r.start=round3(clamp(r.start,0,duration));
  r.end=round3(clamp(r.end,0,duration));
  if(r.end<=r.start){
    if(r.start<duration) r.end=round3(duration);
    else { r.start=0; r.end=round3(duration); }
  }
  const lim=r.effect==='mosaic'?[2,64]:[1,50];
  r.strength=clamp(Math.round(r.strength)||lim[0], lim[0], lim[1]);
}

/* ---------- 좌표 변환 — 모든 화면px ↔ 원본px 변환은 여기로 통일 ----------
   <video>는 object-fit:contain 이라 레터박스가 생긴다. videoWidth/videoHeight 와
   getBoundingClientRect() 로 실제 렌더 영역의 오프셋·스케일을 계산하고,
   #overlay 를 그 렌더 영역에 정확히 겹쳐 놓는다. */
function updateGeometry(){
  const vw=video.videoWidth, vh=video.videoHeight;
  if(!vw||!vh){ overlay.style.display='none'; geo={scale:0,ox:0,oy:0}; return; }
  const r=video.getBoundingClientRect(), wr=wrap.getBoundingClientRect();
  const scale=Math.min(r.width/vw, r.height/vh);
  const dw=vw*scale, dh=vh*scale;
  geo={scale, ox:(r.left-wr.left)+(r.width-dw)/2, oy:(r.top-wr.top)+(r.height-dh)/2};
  overlay.style.display='block';
  overlay.style.left=geo.ox+'px'; overlay.style.top=geo.oy+'px';
  overlay.style.width=dw+'px'; overlay.style.height=dh+'px';
}
function clientToVideo(cx,cy){
  const or=overlay.getBoundingClientRect();
  return { x:clamp((cx-or.left)/geo.scale,0,vidW()), y:clamp((cy-or.top)/geo.scale,0,vidH()) };
}

/* ---------- 사각형 렌더링 ---------- */
function makeRectEl(r){
  const el=document.createElement('div');
  el.className='region'; el.dataset.id=r.id;
  const cv=document.createElement('canvas'); cv.className='fx'; el.appendChild(cv);
  const lb=document.createElement('span'); lb.className='rlabel'; el.appendChild(lb);
  for(const d of ['nw','n','ne','e','se','s','sw','w']){
    const h=document.createElement('div'); h.className='handle h-'+d; h.dataset.dir=d; el.appendChild(h);
  }
  return el;
}
function positionRect(r){
  const el=rectEls.get(r.id); if(!el||!geo.scale) return;
  el.style.left=(r.x*geo.scale)+'px'; el.style.top=(r.y*geo.scale)+'px';
  el.style.width=(r.w*geo.scale)+'px'; el.style.height=(r.h*geo.scale)+'px';
}
function positionAllRects(){ regions.forEach(positionRect); }
function renderRects(){
  const seen=new Set();
  regions.forEach((r,i)=>{
    let el=rectEls.get(r.id);
    if(!el){ el=makeRectEl(r); rectEls.set(r.id,el); overlay.appendChild(el); }
    el.classList.toggle('selected', r.id===selectedId);
    el.querySelector('.rlabel').textContent='#'+(i+1)+' '+(r.effect==='mosaic'?'모자이크':'블러');
    positionRect(r); seen.add(r.id);
  });
  for(const [id,el] of [...rectEls]) if(!seen.has(id)){ el.remove(); rectEls.delete(id); }
}

function select(id){
  if(selectedId===id) return;
  selectedId=id; renderRects();
  for(const item of listEl.children) item.classList.toggle('selected', +item.dataset.id===id);
  const item=listEl.querySelector('.item[data-id="'+id+'"]');
  if(item) item.scrollIntoView({block:'nearest'});
}
function removeRegion(id){
  regions=regions.filter(r=>r.id!==id);
  if(selectedId===id) selectedId=null;
  renderRects(); renderList(); saveState();
}

/* ---------- 드래그: 생성 / 이동 / 8방향 리사이즈 ---------- */
function applyResize(r,orig,dir,dx,dy){
  const vw=vidW(), vh=vidH();
  let x=orig.x, y=orig.y, w=orig.w, h=orig.h;
  if(dir.includes('e')) w=clamp(orig.w+dx, 2, vw-orig.x);
  if(dir.includes('s')) h=clamp(orig.h+dy, 2, vh-orig.y);
  if(dir.includes('w')){ const nx=clamp(orig.x+dx, 0, orig.x+orig.w-2); w=orig.x+orig.w-nx; x=nx; }
  if(dir.includes('n')){ const ny=clamp(orig.y+dy, 0, orig.y+orig.h-2); h=orig.y+orig.h-ny; y=ny; }
  r.x=Math.round(x); r.y=Math.round(y); r.w=Math.round(w); r.h=Math.round(h);
}
overlay.addEventListener('pointerdown', e=>{
  if(e.button!==0||!geo.scale) return;
  /* 패널 입력에 포커스가 남아 있으면 먼저 blur -> change/commit 이 동기 실행되어
     미커밋 편집이 드래그 전에 반영되고, 드래그 중 stale 필드가 생기지 않는다
     (아래 preventDefault 가 기본 포커스 이동을 막으므로 명시적으로 처리) */
  if(document.activeElement&&document.activeElement!==document.body) document.activeElement.blur();
  const p=clientToVideo(e.clientX,e.clientY);
  const hEl=e.target.closest('.handle');
  const rEl=e.target.closest('.region');
  if(hEl&&selectedId!=null){
    const r=regions.find(x=>x.id===selectedId); if(!r) return;
    drag={mode:'resize', dir:hEl.dataset.dir, id:r.id, orig:{...r}, start:p};
  } else if(rEl){
    const id=+rEl.dataset.id; select(id);
    const r=regions.find(x=>x.id===id); if(!r) return;
    drag={mode:'move', id, orig:{...r}, start:p};
  } else {
    const r={id:nextId++, x:p.x, y:p.y, w:0, h:0, start:0, end:round3(duration),
             effect:'mosaic', strength:16};
    regions.push(r); renderList(); select(r.id);
    drag={mode:'create', id:r.id, orig:{...r}, start:p};
  }
  overlay.setPointerCapture(e.pointerId);
  e.preventDefault();
});
overlay.addEventListener('pointermove', e=>{
  if(!drag) return;
  const r=regions.find(x=>x.id===drag.id); if(!r) return;
  const p=clientToVideo(e.clientX,e.clientY);
  const dx=p.x-drag.start.x, dy=p.y-drag.start.y;
  if(drag.mode==='create'){
    r.x=Math.round(Math.min(drag.start.x,p.x)); r.y=Math.round(Math.min(drag.start.y,p.y));
    r.w=Math.round(Math.abs(dx)); r.h=Math.round(Math.abs(dy));
  } else if(drag.mode==='move'){
    r.x=Math.round(clamp(drag.orig.x+dx, 0, vidW()-r.w));
    r.y=Math.round(clamp(drag.orig.y+dy, 0, vidH()-r.h));
  } else {
    applyResize(r, drag.orig, drag.dir, dx, dy);
  }
  positionRect(r); syncInputs(r);
});
function endDrag(){
  if(!drag) return;
  const r=regions.find(x=>x.id===drag.id);
  const wasCreate=drag.mode==='create';
  drag=null;
  if(!r) return;
  if(wasCreate&&(r.w<2||r.h<2)){           /* 클릭만 한 경우: 생성 취소 + 선택 해제 */
    regions=regions.filter(x=>x.id!==r.id); selectedId=null;
    renderRects(); renderList(); return;
  }
  normalizeRegionGeom(r);
  positionRect(r); syncInputs(r); renderRects(); saveState();
}
overlay.addEventListener('pointerup', endDrag);
overlay.addEventListener('pointercancel', endDrag);

/* ---------- 우측 패널 ---------- */
function renderList(){
  listEl.innerHTML='';
  regions.forEach((r,i)=>{
    const div=document.createElement('div');
    div.className='item'+(r.id===selectedId?' selected':'');
    div.dataset.id=r.id;
    div.innerHTML=
      '<div class="itemHead">'+
        '<span class="badge">#'+(i+1)+'</span>'+
        '<select class="f-effect"><option value="mosaic">모자이크</option><option value="blur">블러</option></select>'+
        '<button class="f-del" title="영역 삭제 (Delete)">삭제</button>'+
      '</div>'+
      '<div class="row">'+
        '<label>X<input class="f-x" type="number" step="1"></label>'+
        '<label>Y<input class="f-y" type="number" step="1"></label>'+
        '<label>W<input class="f-w" type="number" step="2" min="2"></label>'+
        '<label>H<input class="f-h" type="number" step="2" min="2"></label>'+
      '</div>'+
      '<div class="row">'+
        '<label>시작<input class="f-start" type="number" step="0.001" min="0"></label>'+
        '<button class="f-setS">현재 위치를 시작으로</button>'+
      '</div>'+
      '<div class="row">'+
        '<label>끝&nbsp;&nbsp;<input class="f-end" type="number" step="0.001" min="0"></label>'+
        '<button class="f-setE">현재 위치를 끝으로</button>'+
      '</div>'+
      '<div class="row">'+
        '<label class="grow">강도<input class="f-strength" type="range" step="1"></label>'+
        '<span class="f-strengthVal"></span>'+
      '</div>';
    bindItem(div,r);
    syncItem(div,r);
    listEl.appendChild(div);
  });
  $('emptyHint').style.display=regions.length?'none':'block';
}
/* force=true: 편집 확정(commit) 시점에는 포커스 중인 필드에도 정규화 값을 써서
   표시값 == 실제값을 보장 (Enter 는 blur 없이 change 를 발생시킨다) */
function syncItem(div,r,force){
  const set=(cls,val)=>{const el=div.querySelector(cls); if(force||el!==document.activeElement) el.value=val;};
  set('.f-x',r.x); set('.f-y',r.y); set('.f-w',r.w); set('.f-h',r.h);
  set('.f-start',round3(r.start)); set('.f-end',round3(r.end));
  const eff=div.querySelector('.f-effect');
  if(force||eff!==document.activeElement) eff.value=r.effect;
  const sl=div.querySelector('.f-strength');
  const lim=r.effect==='mosaic'?[2,64]:[1,50];
  sl.min=lim[0]; sl.max=lim[1];
  if(force||sl!==document.activeElement) sl.value=r.strength;
  div.querySelector('.f-strengthVal').textContent=
    r.effect==='mosaic'?('블록 '+r.strength+'px'):('반경 '+r.strength);
}
function syncInputs(r){
  const div=listEl.querySelector('.item[data-id="'+r.id+'"]');
  if(div) syncItem(div,r);
}
function bindItem(div,r){
  const q=s=>div.querySelector(s);
  div.addEventListener('pointerdown',()=>select(r.id));
  const commit=()=>{
    r.x=parseFloat(q('.f-x').value); r.y=parseFloat(q('.f-y').value);
    r.w=parseFloat(q('.f-w').value); r.h=parseFloat(q('.f-h').value);
    r.start=parseFloat(q('.f-start').value); r.end=parseFloat(q('.f-end').value);
    normalizeRegionGeom(r);
    syncItem(div,r,true); positionRect(r); saveState();
  };
  for(const s of ['.f-x','.f-y','.f-w','.f-h','.f-start','.f-end'])
    q(s).addEventListener('change',commit);
  q('.f-effect').addEventListener('change',()=>{
    r.effect=q('.f-effect').value==='blur'?'blur':'mosaic';
    r.strength=r.effect==='blur'?10:16;
    normalizeRegionGeom(r); syncItem(div,r); renderRects(); saveState();
  });
  q('.f-strength').addEventListener('input',()=>{
    r.strength=Math.round(+q('.f-strength').value);
    q('.f-strengthVal').textContent=
      r.effect==='mosaic'?('블록 '+r.strength+'px'):('반경 '+r.strength);
    saveState();
  });
  q('.f-setS').addEventListener('click',()=>{
    r.start=round3(clamp(video.currentTime,0,Math.max(0,duration-0.001)));
    if(r.end<=r.start) r.end=round3(duration);
    syncItem(div,r); saveState();
  });
  q('.f-setE').addEventListener('click',()=>{
    r.end=round3(clamp(video.currentTime,0.001,duration));
    if(r.start>=r.end) r.start=0;
    syncItem(div,r); saveState();
  });
  q('.f-del').addEventListener('click',e=>{ e.stopPropagation(); removeRegion(r.id); });
}

/* ---------- 재생 컨트롤 / 키보드 ---------- */
function togglePlay(){ if(video.paused) video.play(); else video.pause(); }
function stepFrame(d){
  video.pause();
  video.currentTime=clamp(video.currentTime+d/fps, 0, Math.max(0,duration-0.0001));
}
playBtn.addEventListener('click',togglePlay);
$('stepB').addEventListener('click',()=>stepFrame(-1));
$('stepF').addEventListener('click',()=>stepFrame(1));
seekEl.addEventListener('input',()=>{ video.currentTime=+seekEl.value; });
seekEl.addEventListener('pointerdown',()=>seekHeld=true);
window.addEventListener('pointerup',()=>seekHeld=false);
window.addEventListener('pointercancel',()=>seekHeld=false);

document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ closeModal(); return; }
  const t=e.target, tag=(t.tagName||'').toLowerCase();
  const isRange=tag==='input'&&t.type==='range';
  if((tag==='input'&&!isRange)||tag==='textarea'||tag==='select'||t.isContentEditable) return;
  if(e.key===','){ e.preventDefault(); stepFrame(-1); }
  else if(e.key==='.'){ e.preventDefault(); stepFrame(1); }
  else if(e.key==='Delete'||e.key==='Backspace'){
    if(selectedId!=null){ e.preventDefault(); removeRegion(selectedId); }
  }
  else if(e.key===' '){ e.preventDefault(); togglePlay(); }
});

/* ---------- 매 프레임: 시간 표시 + 활성 상태 + 근사 프리뷰 ---------- */
function fmtT(t){
  t=Math.max(0,t||0);
  const m=Math.floor(t/60), s=t-m*60;
  return m+':'+(s<10?'0':'')+s.toFixed(3);
}
function drawApprox(cv,r){
  const dw=Math.max(1,Math.round(r.w*geo.scale)), dh=Math.max(1,Math.round(r.h*geo.scale));
  if(cv.width!==dw) cv.width=dw;
  if(cv.height!==dh) cv.height=dh;
  const ctx=cv.getContext('2d');
  try{
    if(r.effect==='mosaic'){
      const cw=Math.max(1,Math.round(r.w/r.strength)), ch=Math.max(1,Math.round(r.h/r.strength));
      if(osc.width!==cw) osc.width=cw;
      if(osc.height!==ch) osc.height=ch;
      osctx.drawImage(video, r.x,r.y,r.w,r.h, 0,0,cw,ch);
      ctx.imageSmoothingEnabled=false;
      ctx.clearRect(0,0,dw,dh);
      ctx.drawImage(osc, 0,0,cw,ch, 0,0,dw,dh);
    } else {
      ctx.clearRect(0,0,dw,dh);
      ctx.filter='blur('+Math.max(1,r.strength*geo.scale*0.6)+'px)';
      ctx.drawImage(video, r.x,r.y,r.w,r.h, 0,0,dw,dh);
      ctx.filter='none';
    }
  }catch(err){ /* 시킹 직후 등 일시적 실패는 무시 */ }
}
function tick(){
  const t=video.currentTime;
  if(!seekHeld) seekEl.value=t;
  timeLabel.textContent=fmtT(t)+' / '+fmtT(duration);
  playBtn.textContent=video.paused?'▶':'⏸';
  for(const r of regions){
    const el=rectEls.get(r.id); if(!el) continue;
    const active=t>=r.start-0.001&&t<=r.end+0.001;
    el.classList.toggle('inactive',!active);
    const cv=el.querySelector('canvas.fx');
    if(approxOn&&active&&video.readyState>=2&&geo.scale>0){
      drawApprox(cv,r); cv.style.display='block';
    } else cv.style.display='none';
  }
  requestAnimationFrame(tick);
}

/* ---------- 서버 호출 ---------- */
function payloadRegions(){ return regions.map(stripRegion); }
async function postJSON(url,body){
  const res=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(body)});
  if(!res.ok){
    let msg='HTTP '+res.status;
    try{ const j=await res.json(); if(j.error) msg=j.error; }catch(e){}
    throw new Error(msg);
  }
  return res;
}

$('exactBtn').addEventListener('click', async()=>{
  const btn=$('exactBtn'); btn.disabled=true;
  const old=btn.textContent; btn.textContent='생성 중…';
  const t=video.currentTime;
  try{
    const res=await postJSON('/preview',{t, regions:payloadRegions()});
    const url=URL.createObjectURL(await res.blob());
    const img=new Image(); img.className='previewImg'; img.src=url;
    img.addEventListener('load',()=>URL.revokeObjectURL(url),{once:true});
    openModal('정확한 프리뷰 — t='+t.toFixed(3)+'s (실제 ffmpeg 필터 체인)', img);
  }catch(err){ showError('프리뷰 실패:\n'+err.message); }
  finally{ btn.disabled=false; btn.textContent=old; }
});

$('copyBtn').addEventListener('click', async()=>{
  try{
    const res=await postJSON('/command',{regions:payloadRegions()});
    const j=await res.json();
    let copied=false;
    try{ await navigator.clipboard.writeText(j.command); copied=true; }catch(e){}
    const ta=document.createElement('textarea');
    ta.className='cmdBox'; ta.readOnly=true; ta.value=j.command;
    openModal(copied?'명령어 (클립보드에 복사됨)':'명령어 (아래에서 직접 복사하세요)', ta);
    if(copied) toast('클립보드에 복사했습니다');
    else { ta.focus(); ta.select(); }
  }catch(err){ showError('명령어 생성 실패:\n'+err.message); }
});

function setStatus(msg,isErr){
  statusText.textContent=msg; statusText.title=msg;
  statusText.classList.toggle('err',!!isErr);
}
function fmtDur(s){
  s=Math.max(0,Math.round(s));
  return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
}
$('exportBtn').addEventListener('click', async()=>{
  if(exporting) return;
  if(regions.length===0){ toast('영역이 없습니다 — 영상 위에서 드래그해 영역을 만드세요'); return; }
  exporting=true;
  const btn=$('exportBtn'); btn.disabled=true;
  progressBar.classList.remove('err'); progressBar.style.width='0%';
  setStatus('내보내기 시작…');
  let jobId;
  try{
    const res=await postJSON('/export',{regions:payloadRegions()});
    jobId=(await res.json()).job_id;
  }catch(err){
    showError('내보내기 실패:\n'+err.message);
    setStatus('실패',true); exporting=false; btn.disabled=false; return;
  }
  const timer=setInterval(async()=>{
    let p;
    try{ p=await (await fetch('/progress?job='+encodeURIComponent(jobId))).json(); }
    catch(e){ return; }   /* 일시적 네트워크 오류는 다음 폴링에서 재시도 */
    progressBar.style.width=clamp(p.percent||0,0,100)+'%';
    if(!p.done) setStatus('내보내는 중 '+(p.percent||0).toFixed(1)+'% · '+fmtDur(p.elapsed));
    if(p.done){
      clearInterval(timer); exporting=false; btn.disabled=false;
      if(p.error){
        progressBar.classList.add('err');
        setStatus('내보내기 실패',true);
        showError('ffmpeg 오류:\n'+p.error);
      } else {
        progressBar.style.width='100%';
        setStatus('완료: '+p.output_path);
        toast('내보내기 완료 — '+p.output_path);
      }
    }
  },500);
});

$('approxToggle').addEventListener('change',e=>{ approxOn=e.target.checked; saveState(); });

/* ---------- 모달 / 토스트 ---------- */
function openModal(title,node){
  $('modalTitle').textContent=title;
  const body=$('modalBody'); body.innerHTML=''; body.appendChild(node);
  $('modal').hidden=false;
}
function closeModal(){ $('modal').hidden=true; $('modalBody').innerHTML=''; }
$('modalClose').addEventListener('click',closeModal);
$('modal').addEventListener('click',e=>{ if(e.target===$('modal')) closeModal(); });
function showError(msg){
  const pre=document.createElement('pre'); pre.className='errBox'; pre.textContent=msg;
  openModal('오류',pre);
}
let toastTimer=null;
function toast(msg){
  const t=$('toast'); t.textContent=msg; t.hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>{t.hidden=true;},2600);
}

/* ---------- 초기화 ---------- */
(async function init(){
  try{
    meta=await (await fetch('/meta')).json();
  }catch(e){
    setStatus('메타데이터 로드 실패 — 서버가 종료되었을 수 있습니다',true); return;
  }
  duration=meta.duration; fps=meta.fps>0?meta.fps:30;
  seekEl.max=duration;
  $('fileInfo').textContent=' — '+meta.input_name+' · '
    +meta.display_width+'×'+meta.display_height
    +(meta.rotation?(' (회전 '+meta.rotation+'°)'):'')
    +' · '+meta.fps.toFixed(2)+'fps · '+meta.duration.toFixed(2)+'s · '
    +(meta.has_audio?'오디오 있음':'오디오 없음');
  loadState();
  renderList();

  function onMeta(){
    if(meta.display_width&&video.videoWidth&&video.videoWidth!==meta.display_width){
      console.warn('회전 보정 불일치: 브라우저 '+video.videoWidth+'×'+video.videoHeight
        +' vs 서버 '+meta.display_width+'×'+meta.display_height);
    }
    regions.forEach(normalizeRegionGeom);   /* 저장된 상태를 실제 해상도에 맞춰 클램프 */
    updateGeometry(); renderRects(); renderList();
  }
  if(video.readyState>=1) onMeta();
  else video.addEventListener('loadedmetadata', onMeta, {once:true});

  new ResizeObserver(()=>{ updateGeometry(); positionAllRects(); }).observe(wrap);
  window.addEventListener('resize',()=>{ updateGeometry(); positionAllRects(); });

  requestAnimationFrame(tick);
})();

/* 자동 테스트/디버깅용 훅 */
window.__me={
  get regions(){ return regions; },
  geo:()=>geo, meta:()=>meta,
  addRegion(o){
    const r={id:nextId++, x:0,y:0,w:2,h:2, start:0, end:duration,
             effect:'mosaic', strength:16, ...o};
    normalizeRegionGeom(r); regions.push(r);
    renderRects(); renderList(); select(r.id); saveState(); return r;
  },
  select, removeRegion, updateGeometry
};
</script>
</body>
</html>
"""
PAGE_BYTES = PAGE.encode("utf-8")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------


def detect_script_option():
    """이 ffmpeg 가 -filter_complex_script 를 지원하는지 (9.0 에서 제거됨)."""
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-h", "full"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True  # 감지 실패 시 기존 옵션 가정
    return "filter_complex_script" in out


def main():
    global FFMPEG, FFPROBE, INPUT_PATH, META, HAS_SCRIPT_OPT

    ap = argparse.ArgumentParser(
        description="로컬 전용 영상 모자이크/블러 편집기 (단일 파일, stdlib 전용)"
    )
    ap.add_argument("input", help="입력 영상 파일 경로")
    ap.add_argument("--port", type=int, default=0,
                    help="포트 (기본: 사용 가능한 포트 자동 선택)")
    ap.add_argument("--no-browser", action="store_true",
                    help="브라우저 자동 열기 끄기")
    args = ap.parse_args()

    FFMPEG = shutil.which("ffmpeg")
    FFPROBE = shutil.which("ffprobe")
    if not FFMPEG or not FFPROBE:
        missing = " / ".join(n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not p)
        sys.exit(
            f"오류: PATH 에서 {missing} 을(를) 찾을 수 없습니다.\n"
            "ffmpeg 를 설치한 뒤 다시 실행하세요. (macOS: brew install ffmpeg / "
            "Ubuntu: apt install ffmpeg)"
        )

    HAS_SCRIPT_OPT = detect_script_option()

    INPUT_PATH = os.path.abspath(args.input)
    if not os.path.isfile(INPUT_PATH):
        sys.exit(f"오류: 입력 파일이 없습니다: {INPUT_PATH}")

    META = probe_media(INPUT_PATH)

    rot = f" (회전 {META['rotation']}° → 표시 {META['display_width']}×{META['display_height']})" \
        if META["rotation"] else ""
    print(f"입력: {INPUT_PATH}")
    print(f"영상: {META['width']}×{META['height']}{rot} · "
          f"{META['fps']:.2f}fps · {META['duration']:.2f}s · "
          f"{'오디오 있음' if META['has_audio'] else '오디오 없음'}")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"오류: 서버를 시작할 수 없습니다 (포트 {args.port}): {e}")
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"서버: {url}  (Ctrl+C 로 종료)")

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다…")
    finally:
        # 진행 중인 내보내기 정리: ffmpeg 종료 -> 워커 스레드가 부분 출력물/임시
        # 스크립트를 지울 시간을 준 뒤, 그래도 남은 잔여물은 메인이 직접 지운다.
        with JOBS_LOCK:
            active = [j for j in JOBS.values() if not j["done"]]
        for j in active:
            p = j.get("proc")
            if p and p.poll() is None:
                p.terminate()
        for j in active:
            p = j.get("proc")
            if p:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        deadline = time.time() + 5
        for j in active:
            while not j["done"] and time.time() < deadline:
                time.sleep(0.05)
            if not j["done"]:
                for leftover in (j.get("script_path"), j.get("output_path")):
                    if leftover and os.path.exists(leftover) and \
                            os.path.abspath(leftover) != os.path.abspath(INPUT_PATH):
                        try:
                            os.remove(leftover)
                        except OSError:
                            pass
        server.server_close()


if __name__ == "__main__":
    main()
