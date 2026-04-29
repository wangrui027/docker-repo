package main

import (
    "context"
    "flag"
    "log"
    "net/http"
    "os"
    "os/signal"
    "syscall"
    "time"

    "github.com/lionsoul2014/ip2region/binding/golang/service"
)

func main() {
    // 命令行参数配置
    var (
        port          = flag.String("port", "8080", "HTTP 服务监听端口")
        xdbPath       = flag.String("xdb", "./data/ip2region.xdb", "ip2region v4 xdb 文件路径")
        cacheStrategy = flag.String("cache", "buffer", "缓存策略: none / vindex / buffer")
    )
    flag.Parse()

    // 解析缓存策略
    var cache int
    switch *cacheStrategy {
    case "none":
        cache = service.NoCache
    case "vindex":
        cache = service.VIndexCache
    case "buffer":
        cache = service.BufferCache
    default:
        log.Fatalf("无效的缓存策略: %s, 可选值: none/vindex/buffer", *cacheStrategy)
    }

    // 初始化 ip2region
    log.Printf("正在初始化 ip2region，xdb 路径: %s，缓存策略: %s", *xdbPath, *cacheStrategy)
    if err := InitIp2Region(*xdbPath, cache); err != nil {
        log.Fatalf("初始化 ip2region 失败: %v", err)
    }
    defer CloseIp2Region()

    // 注册路由（使用中间件包装）
    mux := http.NewServeMux()
    mux.HandleFunc("/query", loggingMiddleware(queryIPHandler))
    mux.HandleFunc("/health", loggingMiddleware(healthHandler))

    // 创建 HTTP 服务
    server := &http.Server{
        Addr:         ":" + *port,
        Handler:      mux,
        ReadTimeout:  5 * time.Second,
        WriteTimeout: 10 * time.Second,
        IdleTimeout:  60 * time.Second,
    }

    // 启动 HTTP 服务（在 goroutine 中运行）
    go func() {
        log.Printf("IP 归属地查询服务已启动，监听端口 %s，缓存策略: %s", *port, *cacheStrategy)
        log.Printf("使用示例: curl 'http://localhost:%s/query?ip=8.8.8.8'", *port)
        if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
            log.Fatalf("HTTP 服务启动失败: %v", err)
        }
    }()

    // 优雅关闭
    gracefulShutdown(server)
}

// 优雅关闭逻辑
func gracefulShutdown(server *http.Server) {
    // 监听系统信号
    quit := make(chan os.Signal, 1)
    signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
    <-quit

    log.Println("收到关闭信号，正在优雅关闭服务...")

    // 设置关闭超时（ip2region Close 默认等待 10 秒）
    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()

    // 关闭 HTTP 服务
    if err := server.Shutdown(ctx); err != nil {
        log.Printf("HTTP 服务关闭异常: %v", err)
    }

    // 关闭 ip2region 服务（自动等待查询器归还）
    CloseIp2Region()

    log.Println("服务已安全退出")
}
