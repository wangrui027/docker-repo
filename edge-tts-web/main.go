package main

import (
    "encoding/json"
    "fmt"
    "io"
    "net"
    "net/http"
    "net/url"
    "os"
    "os/exec"
    "strings"
    "sync"
    "time"
)

// 全局配置
var (
    apiToken     string
    defaultVoice string
    voicesCache  []Voice
    cacheMutex   sync.RWMutex
    cacheOnce    sync.Once
)

type Voice struct {
    Name      string `json:"name"`
    Gender    string `json:"gender"`
    Languages string `json:"languages"`
}

func getClientIP(r *http.Request) string {
    // 优先取 X-Forwarded-For 的第一个 IP（最原始客户端）
    if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
        ips := strings.Split(xff, ",")
        if len(ips) > 0 {
            return strings.TrimSpace(ips[0])
        }
    }
    // 其次取 X-Real-IP
    if xri := r.Header.Get("X-Real-IP"); xri != "" {
        return xri
    }
    // 最后回退到 RemoteAddr（但要截掉端口号）
    host, _, err := net.SplitHostPort(r.RemoteAddr)
    if err != nil {
        return r.RemoteAddr
    }
    return host
}

// 日志中间件
func loggingMiddleware(next http.HandlerFunc) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        rw := &responseWriter{w, http.StatusOK}
        next.ServeHTTP(rw, r)

        decodedPath := r.URL.Path
        if query := r.URL.RawQuery; query != "" {
            if decodedQuery, err := url.QueryUnescape(query); err == nil {
                decodedQuery = strings.ReplaceAll(decodedQuery, "+", " ")
                decodedPath = decodedPath + "?" + decodedQuery
            }
        }

        clientIP := getClientIP(r)
        fmt.Printf("%s - [%s] \"%s %s HTTP/1.1\" %d\n",
            clientIP,
            time.Now().Format("02/Jan/2006 15:04:05"),
            r.Method,
            decodedPath,
            rw.statusCode,
        )
    }
}

type responseWriter struct {
    http.ResponseWriter
    statusCode int
}

func (rw *responseWriter) WriteHeader(code int) {
    rw.statusCode = code
    rw.ResponseWriter.WriteHeader(code)
}

// Token 验证
func verifyToken(header http.Header) error {
    if apiToken == "" {
        return nil
    }
    token := header.Get("X-API-Token")
    if token == "" || token != apiToken {
        return fmt.Errorf("invalid or missing API token")
    }
    return nil
}

// 语音列表缓存加载（仅一次）
func loadVoicesCache() {
    cacheOnce.Do(func() {
        cmd := exec.Command("edge-tts", "--list-voices")
        out, err := cmd.Output()
        if err != nil {
            fmt.Printf("⚠️  Failed to load voice list: %v\n", err)
            voicesCache = []Voice{}
            return
        }
        lines := strings.Split(string(out), "\n")
        var voices []Voice
        startParsing := false
        for _, line := range lines {
            if !startParsing {
                if strings.Contains(line, "Name") && strings.Contains(line, "Gender") {
                    startParsing = true
                }
                continue
            }
            line = strings.TrimSpace(line)
            if line == "" {
                continue
            }
            fields := strings.Fields(line)
            if len(fields) < 2 {
                continue
            }
            name := fields[0]
            gender := fields[1]
            lang := "Unknown"
            if dashIdx := strings.Index(name, "-"); dashIdx > 0 {
                lang = name[:dashIdx]
            }
            voices = append(voices, Voice{
                Name:      name,
                Gender:    gender,
                Languages: lang,
            })
        }
        voicesCache = voices
        fmt.Printf("✅  Loaded %d voices from edge-tts\n", len(voicesCache))
    })
}

// TTS 处理器（流式输出）
func ttsHandler(w http.ResponseWriter, r *http.Request) {
    // 验证 token
    if err := verifyToken(r.Header); err != nil {
        http.Error(w, err.Error(), http.StatusUnauthorized)
        return
    }

    // 获取参数
    text := r.URL.Query().Get("text")
    if text == "" {
        http.Error(w, "text parameter is required", http.StatusBadRequest)
        return
    }
    voice := r.URL.Query().Get("voice")
    if voice == "" {
        voice = defaultVoice
    }
    rate := r.URL.Query().Get("rate")
    volume := r.URL.Query().Get("volume")
    pitch := r.URL.Query().Get("pitch")

    // 构建 edge-tts 命令
    args := []string{"--text", text, "--voice", voice}
    if rate != "" {
        args = append(args, "--rate", rate)
    }
    if volume != "" {
        args = append(args, "--volume", volume)
    }
    if pitch != "" {
        args = append(args, "--pitch", pitch)
    }
    // 强制输出到 stdout（默认行为）
    cmd := exec.CommandContext(r.Context(), "edge-tts", args...)

    stdout, err := cmd.StdoutPipe()
    if err != nil {
        http.Error(w, fmt.Sprintf("Failed to create stdout pipe: %v", err), http.StatusInternalServerError)
        return
    }
    stderr, err := cmd.StderrPipe()
    if err != nil {
        http.Error(w, fmt.Sprintf("Failed to create stderr pipe: %v", err), http.StatusInternalServerError)
        return
    }

    if err := cmd.Start(); err != nil {
        http.Error(w, fmt.Sprintf("Failed to start edge-tts: %v", err), http.StatusInternalServerError)
        return
    }

    // 设置响应头
    w.Header().Set("Content-Type", "audio/mpeg")
    w.Header().Set("Cache-Control", "no-cache")
    w.Header().Set("Content-Disposition", "inline")

    // 流式拷贝 stdout 到 ResponseWriter
    _, copyErr := io.Copy(w, stdout)
    if copyErr != nil {
        // 客户端可能断开，忽略错误
    }

    // 读取 stderr 用于错误日志（但不阻塞）
    go func() {
        errBytes, _ := io.ReadAll(stderr)
        if len(errBytes) > 0 {
            fmt.Printf("edge-tts stderr: %s\n", string(errBytes))
        }
    }()

    // 等待命令结束
    if err := cmd.Wait(); err != nil {
        // 如果 stdout 已经完整发送完毕，忽略此错误（可能只是 edge-tts 的退出码）
        if copyErr == nil {
            fmt.Printf("edge-tts exited with error: %v\n", err)
        }
    }
}

// 语音列表接口（缓存）
func listVoicesHandler(w http.ResponseWriter, r *http.Request) {
    if err := verifyToken(r.Header); err != nil {
        http.Error(w, err.Error(), http.StatusUnauthorized)
        return
    }
    loadVoicesCache()

    cacheMutex.RLock()
    voices := voicesCache
    cacheMutex.RUnlock()

    response := map[string]interface{}{
        "total":         len(voices),
        "voices":        voices,
        "default_voice": defaultVoice,
    }
    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(response)
}

// 健康检查
func healthHandler(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(map[string]interface{}{
        "status":        "ok",
        "auth_required": apiToken != "",
    })
}

// 根路径重定向到 /docs，其他未匹配路径返回 404
func rootHandler(w http.ResponseWriter, r *http.Request) {
    if r.URL.Path != "/" {
        http.NotFound(w, r)
        return
    }
    http.Redirect(w, r, "/docs", http.StatusFound)
}

// 打印 /docs 提示（因为 Swagger UI 未实现，输出 JSON 说明）
func docsHandler(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(map[string]interface{}{
        "message": "TTS API documentation",
        "endpoints": []string{
            "/api/v1/audio/speech",
            "/api/v1/audio/list-voices",
            "/health",
            "/docs",
        },
    })
}

func main() {
    // 读取配置（环境变量或默认值）
    apiToken = os.Getenv("TTS_API_TOKEN")
    defaultVoice = os.Getenv("DEFAULT_VOICE")
    if defaultVoice == "" {
        defaultVoice = "zh-CN-YunjianNeural"
    }

    fmt.Printf("✅ 默认语音: %s\n", defaultVoice)
    if apiToken != "" {
        fmt.Println("✅ API Token 已配置，请求需要携带 X-API-Token 请求头")
    } else {
        fmt.Println("⚠️  未配置 API Token，接口将无需认证即可访问")
    }

    // 路由注册
    http.HandleFunc("/", loggingMiddleware(rootHandler))
    http.HandleFunc("/docs", loggingMiddleware(docsHandler))
    http.HandleFunc("/api/v1/audio/speech", loggingMiddleware(ttsHandler))
    http.HandleFunc("/api/v1/audio/list-voices", loggingMiddleware(listVoicesHandler))
    http.HandleFunc("/health", loggingMiddleware(healthHandler))

    port := os.Getenv("PORT")
    if port == "" {
        port = "8000"
    }
    fmt.Printf("🚀 Server listening on port %s\n", port)
    if err := http.ListenAndServe(":"+port, nil); err != nil {
        fmt.Printf("Server error: %v\n", err)
        os.Exit(1)
    }
}
