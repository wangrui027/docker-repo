package main

import (
    "encoding/json"
    "log"
    "net"
    "net/http"
    "strings"
    "time"
)

// 统一响应结构
type Response struct {
    Code    int    `json:"code"`
    Message string `json:"message,omitempty"`
    Data    any    `json:"data,omitempty"`
}

// 查询结果结构
type QueryResult struct {
    IP     string `json:"ip"`
    Region string `json:"region"`
}

// 日志中间件：记录请求方法、路径、耗时和客户端 IP
func loggingMiddleware(next http.HandlerFunc) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        start := time.Now()
        // 获取真实客户端 IP（支持代理场景）
        clientIP := getClientIP(r)
        next(w, r)
        log.Printf("[%s] %s %s - %s - %v",
            r.Method,
            r.URL.Path,
            r.URL.RawQuery,
            clientIP,
            time.Since(start),
        )
    }
}

// 获取客户端真实 IP
func getClientIP(r *http.Request) string {
    // 优先取 X-Forwarded-For
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
    // 最后从 RemoteAddr 解析
    ip, _, _ := net.SplitHostPort(r.RemoteAddr)
    return ip
}

// 验证 IP 地址格式
func isValidIP(ip string) bool {
    parsed := net.ParseIP(ip)
    return parsed != nil
}

// IP 查询处理函数
func queryIPHandler(w http.ResponseWriter, r *http.Request) {
    // 只允许 GET 方法
    if r.Method != http.MethodGet {
        writeJSON(w, http.StatusMethodNotAllowed, Response{
            Code:    405,
            Message: "仅支持 GET 方法",
        })
        return
    }

    // 获取查询参数
    ip := r.URL.Query().Get("ip")
    if ip == "" {
        // 未提供 IP 时，返回客户端 IP
        ip = getClientIP(r)
        if ip == "" {
            writeJSON(w, http.StatusBadRequest, Response{
                Code:    400,
                Message: "无法获取客户端 IP，请在请求中带上 ip 参数",
            })
            return
        }
    }

    // 验证 IP 合法性
    if !isValidIP(ip) {
        writeJSON(w, http.StatusBadRequest, Response{
            Code:    400,
            Message: "无效的 IP 地址格式",
        })
        return
    }

    // 查询归属地
    region, err := SearchIP(ip)
    if err != nil {
        writeJSON(w, http.StatusInternalServerError, Response{
            Code:    500,
            Message: "查询失败: " + err.Error(),
        })
        return
    }

    writeJSON(w, http.StatusOK, Response{
        Code: 0,
        Data: QueryResult{
            IP:     ip,
            Region: region,
        },
    })
}

// 健康检查接口
func healthHandler(w http.ResponseWriter, r *http.Request) {
    writeJSON(w, http.StatusOK, Response{
        Code: 0,
        Data: map[string]string{"status": "ok"},
    })
}

// 统一 JSON 响应输出
func writeJSON(w http.ResponseWriter, statusCode int, resp Response) {
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(statusCode)
    json.NewEncoder(w).Encode(resp)
}
