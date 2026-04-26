from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.responses import Response, RedirectResponse
from edge_tts import Communicate
import os
import sys
import subprocess
from urllib.parse import unquote
import time

app = FastAPI()

# 自定义中间件，模拟 Flask 日志格式
@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    
    # 解码 URL 中的中文
    path = unquote(request.url.path)
    
    # 获取原始查询字符串并解码
    query_string = request.scope.get('query_string', b'').decode('utf-8')
    if query_string:
        # 先解码整个查询字符串
        query_string = unquote(query_string)
        full_url = f"{path}?{query_string}"
    else:
        full_url = path
    
    log_message = f'{request.client.host} - - [{time.strftime("%d/%b/%Y %H:%M:%S")}] "{request.method} {full_url} HTTP/1.1" {response.status_code} -'
    
    print(log_message, flush=True)
    
    return response

# 获取 API Token（优先级：命令行参数 > 环境变量）
API_TOKEN = None
DEFAULT_VOICE = 'zh-CN-YunjianNeural'  # 默认值

# 解析命令行参数
for arg in sys.argv[1:]:
    if arg.startswith('--tts-api-token='):
        API_TOKEN = arg.split('=')[1]
    elif arg.startswith('--default-voice='):
        DEFAULT_VOICE = arg.split('=')[1]

# 如果命令行没有，从环境变量获取
if not API_TOKEN:
    API_TOKEN = os.environ.get('TTS_API_TOKEN')
if not DEFAULT_VOICE or DEFAULT_VOICE == 'zh-CN-YunjianNeural':
    env_voice = os.environ.get('DEFAULT_VOICE')
    if env_voice:
        DEFAULT_VOICE = env_voice

# 启动时显示配置
print(f"✅ 默认语音: {DEFAULT_VOICE}")
if API_TOKEN:
    print(f"✅ API Token 已配置，请求需要携带 X-API-Token 请求头")
else:
    print("⚠️  未配置 API Token，接口将无需认证即可访问")

def verify_token(x_api_token: str = Header(None)):
    """验证 API Token"""
    if not API_TOKEN:
        return
    if not x_api_token or x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")

@app.get('/')
async def root():
    """首页重定向到 /docs"""
    return RedirectResponse(url='/docs')

@app.get(
    '/api/v1/audio/speech',
    response_class=Response,
    responses={
        200: {
            "description": "成功返回 MP3 音频流",
            "content": {
                "audio/mpeg": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                        "description": "MP3 音频文件二进制数据"
                    },
                    "example": "二进制音频数据"
                }
            }
        },
        400: {"description": "参数错误"},
        401: {"description": "未授权或 Token 无效"},
        500: {"description": "服务器内部错误或 TTS 生成失败"}
    }
)
async def tts(
    text: str = Query(..., description="要转换为语音的文本内容", min_length=1, max_length=1000),
    voice: str = Query(DEFAULT_VOICE, description="语音名称，可通过 /api/v1/audio/list-voices 查看可用语音"),
    rate: str = Query(None, description="语速调节，格式如: +0%, -10%, +20%", examples=["+0%"]),
    volume: str = Query(None, description="音量调节，格式如: +0%, -20%, +50%", examples=["+0%"]),
    pitch: str = Query(None, description="音调调节，格式如: +0Hz, +5Hz, -10Hz", examples=["+0Hz"]),
    x_api_token: str = Header(None, description="API Token（如果服务配置了认证）")
):
    """
    文本转语音接口 (Text-to-Speech)
    
    将中文文本转换为自然流畅的 MP3 音频文件。支持多种语音、语速、音量和音调调节。
    
    - **text**: 要转换的文本内容（必填）
    - **voice**: 语音名称，默认为 zh-CN-YunjianNeural
    - **rate**: 语速调节，默认为 0%
    - **volume**: 音量调节，默认为 0%
    - **pitch**: 音调调节，默认为 0Hz
    
    返回 MP3 音频流，可直接播放或保存为 .mp3 文件。
    """
    # 验证 token
    verify_token(x_api_token)
    
    # 只传递非 None 的参数
    kwargs = {}
    if rate is not None:
        kwargs['rate'] = rate
    if volume is not None:
        kwargs['volume'] = volume
    if pitch is not None:
        kwargs['pitch'] = pitch
    
    try:
        comm = Communicate(text, voice, **kwargs)
        audio = b''
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        
        if not audio:
            raise HTTPException(status_code=500, detail="No audio data generated")
        
        return Response(
            content=audio,
            media_type='audio/mpeg',
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "no-cache",
                "Content-Type": "audio/mpeg"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS generation failed: {str(e)}")

@app.get('/api/v1/audio/list-voices')
async def list_voices(x_api_token: str = Header(None)):
    verify_token(x_api_token)
    
    try:
        result = subprocess.run(
            ['edge-tts', '--list-voices'],
            capture_output=True,
            text=True,
            check=True
        )
        
        voices = []
        lines = result.stdout.strip().split('\n')
        
        for line in lines[2:]:  # 跳过表头
            if not line.strip():
                continue
            
            # 使用正则或更精确的解析
            parts = line.split()
            if len(parts) >= 2:
                voice_data = {
                    "name": parts[0],
                    "gender": parts[1] if len(parts) > 1 else "Unknown",
                    "languages": parts[0].split('-')[0] if '-' in parts[0] else "Unknown"
                }
                voices.append(voice_data)
        
        return {
            "total": len(voices),
            "voices": voices,
            "default_voice": DEFAULT_VOICE
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/health')
async def health():
    """健康检查接口"""
    return {"status": "ok", "auth_required": bool(API_TOKEN)}
    