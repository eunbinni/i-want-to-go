#!/usr/bin/env python3
"""
1. time.app.fans epochMs로 서버 시각 동기화 (버튼 갱신 기준)
2. 목표 직전 초 경계를 폴링해 오프셋 정밀 보정 (오차 ±10~20ms)
3. 클릭 순간에는 네트워크를 타지 않고 로컬 단조시계로 발사
4. CoreGraphics로 직접 마우스 이벤트

사용법:
  python3 iphone-click.py "2026-08-09 20:00:00"
  python3 iphone-click.py "2026-08-09 20:00:00" --lead 0

옵션:
  --lead N   클릭을 00초보다 N ms 앞당김 (음수면 늦게, 기본 40)

실행 시점의 마우스 커서 위치에 클릭합니다. 미러링 창 위에 커서를 올려둔 뒤 실행하세요.

사전 준비:
  시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용
  → 실행하는 터미널 앱(Terminal / Cursor) 허용
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import http.client
import json
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
TIME_HOST = "time.app.fans"


# ---------------------------------------------------------------- 클릭 (CoreGraphics)

class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_cg():
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventPost.restype = None
    return cg


def current_mouse_pos() -> tuple[int, int]:
    cg = _load_cg()
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    event = cg.CGEventCreate(None)
    pt = cg.CGEventGetLocation(event)
    cf.CFRelease(event)
    return int(pt.x), int(pt.y)


def make_click(x: int, y: int):
    cg = _load_cg()

    pt = CGPoint(float(x), float(y))
    down = cg.CGEventCreateMouseEvent(None, 1, pt, 0)  # kCGEventLeftMouseDown
    up = cg.CGEventCreateMouseEvent(None, 2, pt, 0)    # kCGEventLeftMouseUp

    def fire() -> None:
        cg.CGEventPost(0, down)
        cg.CGEventPost(0, up)

    return fire


# ---------------------------------------------------------------- 서버 시간 (time.app.fans)

def open_conn() -> http.client.HTTPSConnection:
    conn = http.client.HTTPSConnection(TIME_HOST, timeout=3, context=ssl.create_default_context())
    conn.request("GET", "/", headers={"Cache-Control": "no-store"})
    conn.getresponse().read()  # TLS 핸드셰이크와 커넥션을 미리 워밍업
    return conn


def fetch_epoch_ms(conn: http.client.HTTPSConnection) -> tuple[int, float, float]:
    """(epochMs, 요청 송신 시각, 왕복 시간)"""
    t0 = time.monotonic()
    conn.request("GET", "/", headers={"Cache-Control": "no-store"})
    res = conn.getresponse()
    body = res.read()
    t1 = time.monotonic()
    epoch_ms = json.loads(body)["epochMs"]
    return epoch_ms, t0, t1 - t0


def rough_offset(conn: http.client.HTTPSConnection) -> float:
    """서버시간 - 로컬(monotonic) 대략적 오프셋."""
    epoch_ms, t0, rtt = fetch_epoch_ms(conn)
    server_ts = epoch_ms / 1000.0
    return server_ts - (t0 + rtt)


def calibrate_boundary(conn: http.client.HTTPSConnection, deadline_mono: float) -> float | None:
    """epochMs 초가 바뀌는 순간을 잡아 정밀 오프셋을 반환. 실패 시 None."""
    prev_ms, prev_t0, _ = fetch_epoch_ms(conn)
    prev_sec = prev_ms // 1000
    while time.monotonic() < deadline_mono:
        epoch_ms, t0, rtt = fetch_epoch_ms(conn)
        sec = epoch_ms // 1000
        if sec > prev_sec:
            boundary_mono = (prev_t0 + t0) / 2 + rtt / 2
            return sec - boundary_mono
        prev_ms, prev_t0 = epoch_ms, t0
        time.sleep(0.004)
    return None


# ---------------------------------------------------------------- 메인

def parse_args(argv: list[str]) -> tuple[str, int]:
    if len(argv) < 2:
        print(__doc__)
        sys.exit(1)

    args = argv[1:]
    lead_ms = 40
    if "--lead" in args:
        i = args.index("--lead")
        lead_ms = int(args[i + 1])
        args = args[:i] + args[i + 2:]

    if not args:
        print(__doc__)
        sys.exit(1)

    return args[0], lead_ms


def spin_until_mono(deadline: float) -> None:
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        if remain > 0.05:
            time.sleep(remain - 0.03)
        else:
            while time.monotonic() < deadline:
                pass
            return


def main() -> None:
    target_str, lead_ms = parse_args(sys.argv)
    x, y = current_mouse_pos()
    target_ts = datetime.strptime(target_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()

    fire = make_click(x, y)
    conn = open_conn()

    offset = rough_offset(conn)  # monotonic + offset ≈ 서버시간
    remain = target_ts - (time.monotonic() + offset)
    if remain < 3:
        print("목표까지 3초 미만이라 정밀 보정 시간이 부족합니다. 더 일찍 실행하세요.")
        sys.exit(1)
    print(f"목표까지 {remain:.1f}초. ({x}, {y}) lead {lead_ms}ms.")
    print("미러링 창을 움직이지 말고 Mac을 건드리지 마세요.")

    # 목표 6초 전까지 대기 후 초 경계 정밀 측정 (경계는 매초 오므로 2초면 충분)
    spin_until_mono(target_ts - offset - 6)
    precise = calibrate_boundary(conn, deadline_mono=target_ts - offset - 2.5)
    if precise is not None:
        offset = precise
        print(f"초 경계 정밀 보정 완료 (time.app.fans, 오차 ±10~20ms 수준)")
    else:
        print("경계 감지 실패, 근사 오프셋 사용 (오차 최대 ±0.5초)")
    conn.close()

    deadline = target_ts - offset - lead_ms / 1000.0

    gc.disable()  # 발사 직전 GC 멈춤으로 지터 제거
    spin_until_mono(deadline)
    fire()
    gc.enable()

    print(f"클릭 완료: {datetime.now(KST).strftime('%H:%M:%S.%f')[:-3]} "
          f"(서버 기준 약 {(time.monotonic() + offset - target_ts) * 1000:+.0f}ms)")


if __name__ == "__main__":
    main()

