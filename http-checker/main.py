# main.py
import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse  # 新增导入
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

# ---------- 新增：根路径重定向到 /docs ----------
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")

# ---------- 状态测试端点 ----------
@app.get("/status")
async def check_status(
    url: str = Query(..., description="要测试的URL地址")
):
    """
    发送GET请求到指定URL，根据响应状态码判断是否正常。
    状态码是否正常、超时时间、代理地址均由环境变量配置。
    """
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
                "status": "normal" if is_normal else "abnormal",
                "code": status_code,
                "url": url
            }
    except httpx.TimeoutException:
        return {"status": "abnormal", "error": "Request timeout", "url": url}
    except httpx.ConnectError:
        return {"status": "abnormal", "error": "Connection error", "url": url}
    except httpx.RequestError as e:
        return {"status": "abnormal", "error": f"Request error: {str(e)}", "url": url}
    except Exception as e:
        return {"status": "abnormal", "error": f"Unexpected error: {str(e)}", "url": url}

@app.get("/health")
async def health():
    """健康检查端点"""
    return {"status": "ok"}
    