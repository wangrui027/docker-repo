# Edge-TTS-Web

基于 `FastAPI` 和 `edge-tts` 的文本转语音 Web 服务，将文本转换为自然流畅的 MP3 音频。支持多种语音、语速、音量和音调调节，并提供可选的 API 认证。

> 使用微软 Edge 浏览器相同的 TTS 技术，无需 Azure 订阅。

## 功能特点

- 🎤 高质量免费 TTS，支持多语言（中文、英文等）
- 🚀 异步高性能 API，基于 FastAPI
- 🐳 轻量级 Docker 镜像（基于 Alpine）
- 🔐 可选 API Token 认证
- 🎚️ 可调节语速、音量、音调
- 📋 查询所有可用语音列表
- 💾 直接返回 MP3 音频流，无需保存文件

## 快速开始

### Docker 运行（推荐）

```bash
# 克隆仓库
git clone https://github.com/wangrui027/edge-tts-web.git
cd edge-tts-web

# 构建镜像
docker build -t edge-tts-web .

# 运行容器（无认证）
docker run -d -p 8000:8000 --name tts edge-tts-web

# 运行容器（带 API Token 认证）
docker run -d -p 8000:8000 -e TTS_API_TOKEN=your-secret-token --name tts edge-tts-web
```

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务（无认证）
python main.py

# 带认证启动
python main.py --tts-api-token=your-secret-token --default-voice=zh-CN-XiaoxiaoNeural

# 或通过环境变量
export TTS_API_TOKEN=your-secret-token
python main.py
```

服务启动后，访问 `http://localhost:8000/docs` 查看自动生成的 API 文档（Swagger UI）。

## API 接口

### 1. 文本转语音

```bash
GET /api/v1/audio/speech
```

| 参数     | 类型   | 必填 | 默认值                | 描述                        |
| :------- | :----- | :--- | :-------------------- | :-------------------------- |
| `text`   | string | ✅    | 无                    | 要转换的文本（1-1000字符）  |
| `voice`  | string | ❌    | `zh-CN-YunjianNeural` | 语音名称                    |
| `rate`   | string | ❌    | +0%                   | 语速，格式如 `+10%`, `-5%`  |
| `volume` | string | ❌    | +0%                   | 音量，格式如 `+20%`, `-10%` |
| `pitch`  | string | ❌    | +0Hz                  | 音调，格式如 `+5Hz`, `-2Hz` |

**请求示例：**

bash

```bash
# 无认证
curl "http://localhost:8000/api/v1/audio/speech?text=你好世界" --output hello.mp3

# 带认证
curl -H "X-API-Token: your-secret-token" \
  "http://localhost:8000/api/v1/audio/speech?text=欢迎使用&rate=+10%" \
  --output welcome.mp3
```

**响应：** MP3 音频文件（audio/mpeg）

### 2. 获取语音列表

```bash
GET /api/v1/audio/list-voices
```

```bash
curl "http://localhost:8000/api/v1/audio/list-voices"
```

响应示例：

```json
{
  "total": 200,
  "voices": [
    { "name": "zh-CN-YunjianNeural", "gender": "Male", "languages": "zh" },
    { "name": "zh-CN-XiaoxiaoNeural", "gender": "Female", "languages": "zh" }
  ],
  "default_voice": "zh-CN-YunjianNeural"
}
```

### 3. 健康检查

```bash
GET /health
```

```bash
curl "http://localhost:8000/health"
# {"status":"ok","auth_required":false}
```

## 认证配置

如果需要保护接口，可通过以下方式设置 API Token：

- **环境变量**：`TTS_API_TOKEN=your-token`
- **命令行参数**：`--tts-api-token=your-token`

启用认证后，所有需要鉴权的接口必须在请求头中包含：

```bash
X-API-Token: your-secret-token
```

## 自定义默认语音

修改默认语音的方法（优先级从高到低）：

1. 命令行参数：`python main.py --default-voice=zh-CN-XiaoxiaoNeural`
2. 环境变量：`DEFAULT_VOICE=zh-CN-XiaoxiaoNeural`
3. 代码内默认值：`zh-CN-YunjianNeural`

## 常见问题

**Q：支持哪些语言？**
A：支持所有 Edge 浏览器 TTS 支持的语言，包括中文（普通话、粤语）、英语（美式、英式）、日语、韩语等。通过 `/api/v1/audio/list-voices` 查看完整列表。

**Q：文本长度限制？**
A：API 限制每次请求最多 1000 字符，如需更长文本请自行分段处理。

**Q：Docker 容器日志显示中文乱码？**
A：这是终端编码问题，不影响实际音频内容。可设置环境变量 `PYTHONIOENCODING=utf-8`。

**Q：如何修改服务端口？**
A：修改 `CMD` 中的 `--port` 参数，或通过 `docker run -p 宿主机端口:8000` 映射。

