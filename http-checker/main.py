# main.py
import os
import asyncio
from typing import List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import httpx

app = FastAPI(title="HTTP Status Checker", description="检查URL返回的状态码是否正常")

# 默认允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 环境变量配置 ----------
def _parse_status_codes(env_var: str, default: set) -> set:
    """解析环境变量中的状态码列表，格式如 '200,302,307,401,403'"""
    value = os.getenv(env_var)
    if not value:
        return default
    try:
        codes = set(int(code.strip()) for code in value.split(',') if code.strip())
        return codes if codes else default
    except ValueError:
        return default

NORMAL_STATUS_CODES = _parse_status_codes("NORMAL_STATUS_CODES", {200, 302, 307, 401, 403})
TIMEOUT = float(os.getenv("TIMEOUT", "5.0"))
PROXY = os.getenv("PROXY")  # 可为 None 或 http://proxy.example.com:8080
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "50"))  # 批量检测最大数量限制

# ---------- 辅助函数：单 URL 检测 ----------
async def _check_single_url(url: str) -> Dict[str, Any]:
    """执行单个 URL 的检测，返回结构化结果"""
    try:
        client_kwargs = {
            "timeout": TIMEOUT,
            "follow_redirects": False,
        }
        if PROXY:
            client_kwargs["proxy"] = PROXY

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url)
            status_code = response.status_code
            is_normal = status_code in NORMAL_STATUS_CODES
            return {
                "url": url,
                "status": "normal" if is_normal else "abnormal",
                "code": status_code,
            }
    except httpx.TimeoutException:
        return {"url": url, "status": "abnormal", "error": "Request timeout"}
    except httpx.ConnectError:
        return {"url": url, "status": "abnormal", "error": "Connection error"}
    except httpx.RequestError as e:
        return {"url": url, "status": "abnormal", "error": f"Request error: {str(e)}"}
    except Exception as e:
        return {"url": url, "status": "abnormal", "error": f"Unexpected error: {str(e)}"}

# ---------- 路由 ----------
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")

@app.get("/status")
async def check_status(url: str = Query(..., description="要测试的URL地址")):
    """
    发送GET请求到指定URL，根据响应状态码判断是否正常。
    状态码是否正常、超时时间、代理地址均由环境变量配置。
    """
    return await _check_single_url(url)

@app.post("/status/batch")
async def batch_check_status(urls: List[str]) -> Dict[str, Any]:
    """
    批量检测 URL 状态。
    请求体格式: ["https://example1.com", "https://example2.com", ...]
    """
    if not urls:
        raise HTTPException(status_code=400, detail="URL list cannot be empty")

    if len(urls) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size exceeds limit: {MAX_BATCH_SIZE}"
        )

    # 可选：去除重复 URL（保留顺序）
    unique_urls = list(dict.fromkeys(urls))

    # 并发执行所有检测
    tasks = [_check_single_url(url) for url in unique_urls]
    results = await asyncio.gather(*tasks)

    normal_count = sum(1 for r in results if r["status"] == "normal")
    abnormal_count = len(results) - normal_count

    return {
        "total": len(results),
        "normal_count": normal_count,
        "abnormal_count": abnormal_count,
        "details": results
    }

@app.get("/health")
async def health():
    """健康检查端点"""
    return {"status": "ok"}
    