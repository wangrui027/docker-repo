#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import os
import subprocess
import tempfile
import threading
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, ConfigDict

CFST_PATH = os.environ.get("CFST_PATH", "./CloudflareSpeedTest.exe")
CFST_WORKDIR = os.environ.get("CFST_WORKDIR", os.path.dirname(os.path.abspath(__file__)))
CFST_TIMEOUT = int(os.environ.get("CFST_TIMEOUT", "600"))

app = FastAPI(title="CloudflareSpeedTest API", version="1.0.0")
_speedtest_lock = threading.Lock()


class SpeedResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ip: str = Field(..., alias="ip")
    sent: int = Field(..., alias="sent")
    received: int = Field(..., alias="received")
    loss_rate: float = Field(..., alias="loss_rate")
    avg_latency: float = Field(..., alias="avg_latency")
    download_speed: float = Field(..., alias="download_speed")
    colo: Optional[str] = Field(None, alias="cfcolo")


class SpeedTestResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    success: bool
    message: str
    params: dict
    count: int
    results: list[SpeedResult]


def _build_args(dn, dt, url, cfcolo, tl, tll, tlr, sl, ip, temp_csv):
    args = [CFST_PATH, "-o", temp_csv, "-p", "0"]
    if dn is not None: args.extend(["-dn", str(dn)])
    if dt is not None: args.extend(["-dt", str(dt)])
    if url is not None: args.extend(["-url", url])
    if cfcolo is not None: args.extend(["-cfcolo", cfcolo])
    if tl is not None: args.extend(["-tl", str(tl)])
    if tll is not None: args.extend(["-tll", str(tll)])
    if tlr is not None: args.extend(["-tlr", str(tlr)])
    if sl is not None: args.extend(["-sl", str(sl)])
    if ip is not None: args.extend(["-ip", ip])
    return args


def _parse_result(csv_path: str) -> list[dict]:
    results = []
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return results

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                speed = float((row.get("下载速度(MB/s)", "0") or "0").strip())
            except (ValueError, TypeError):
                speed = 0.0

            if speed > 0:
                results.append({
                    "ip": row.get("IP 地址", "").strip(),
                    "sent": int(row.get("已发送", "0") or "0"),
                    "received": int(row.get("已接收", "0") or "0"),
                    "loss_rate": float(row.get("丢包率", "0") or "0"),
                    "avg_latency": float(row.get("平均延迟", "0") or "0"),
                    "download_speed": speed,
                    "cfcolo": (row.get("地区码", "") or "").strip() or None,
                })

    return results

@app.get('/')
async def root():
    """首页重定向到 /docs"""
    return RedirectResponse(url='/docs')

@app.get("/speedtest", response_model=SpeedTestResponse)
def speedtest(
        dn: Optional[int] = Query(None, ge=1, le=100),
        dt: Optional[int] = Query(None, ge=1, le=60),
        url: Optional[str] = Query(None),
        cfcolo: Optional[str] = Query(None),
        tl: Optional[int] = Query(None, ge=1, le=9999),
        tll: Optional[int] = Query(None, ge=0, le=9999),
        tlr: Optional[float] = Query(None, ge=0.0, le=1.0),
        sl: Optional[float] = Query(None, ge=0.0),
        sr: Optional[float] = Query(None, ge=0.0),
        ip: Optional[str] = Query(None),
):
    if not _speedtest_lock.acquire(blocking=False):
        raise HTTPException(status_code=423, detail={
            "success": False, "message": "当前已有测速任务正在运行，请等待完成后再试",
            "params": {}, "count": 0, "results": []
        })

    speed_limit = sl if sl is not None else sr
    query_params = {k: v for k, v in {
        "dn": dn, "dt": dt, "url": url, "cfcolo": cfcolo,
        "tl": tl, "tll": tll, "tlr": tlr, "sl": speed_limit, "ip": ip
    }.items() if v is not None}

    temp_csv = None
    try:
        fd, temp_csv = tempfile.mkstemp(suffix=".csv", prefix="cfst_")
        os.close(fd)

        cmd = _build_args(dn, dt, url, cfcolo, tl, tll, tlr, speed_limit, ip, temp_csv)
        subprocess.run(
            cmd, cwd=CFST_WORKDIR, capture_output=True,
            text=True, timeout=CFST_TIMEOUT, encoding="utf-8", errors="replace"
        )

        results = _parse_result(temp_csv)
        return {
            "success": True, "message": "测速完成",
            "params": query_params, "count": len(results), "results": results
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail={
            "success": False, "message": f"测速超时（超过 {CFST_TIMEOUT} 秒）",
            "params": query_params, "count": 0, "results": []
        })
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail={
            "success": False, "message": f"未找到可执行文件: {CFST_PATH}",
            "params": query_params, "count": 0, "results": []
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail={
            "success": False, "message": f"测速异常: {str(e)}",
            "params": query_params, "count": 0, "results": []
        })
    finally:
        if temp_csv and os.path.exists(temp_csv):
            try:
                os.remove(temp_csv)
            except OSError:
                pass
        _speedtest_lock.release()


@app.get("/health")
def health():
    exists = os.path.isfile(CFST_PATH)
    busy = not _speedtest_lock.acquire(blocking=False)
    if not busy:
        _speedtest_lock.release()
    return {"status": "ok" if exists else "error", "cfst_exists": exists, "busy": busy}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
