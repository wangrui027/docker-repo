package main

import (
    "crypto/tls"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/proxy"
)

// ---------- 配置结构 ----------
type Config struct {
	NormalStatusCodes map[int]bool
	Timeout           time.Duration
	Proxy             string
	MaxBatchSize      int
	MaxConcurrent     int
}

var config Config
var httpClient *http.Client

// ---------- 解析环境变量 ----------
func parseStatusCodes(envVar string, defaultCodes []int) map[int]bool {
	val := os.Getenv(envVar)
	if val == "" {
		codesMap := make(map[int]bool)
		for _, code := range defaultCodes {
			codesMap[code] = true
		}
		return codesMap
	}
	parts := strings.Split(val, ",")
	codesMap := make(map[int]bool)
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		code, err := strconv.Atoi(p)
		if err == nil {
			codesMap[code] = true
		}
	}
	if len(codesMap) == 0 {
		// fallback to default
		for _, code := range defaultCodes {
			codesMap[code] = true
		}
	}
	return codesMap
}

func getEnvInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	i, err := strconv.Atoi(val)
	if err != nil {
		return defaultVal
	}
	return i
}

func getEnvFloat(key string, defaultVal float64) float64 {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	f, err := strconv.ParseFloat(val, 64)
	if err != nil {
		return defaultVal
	}
	return f
}

// ---------- 初始化 HTTP 客户端（支持代理） ----------
func initHTTPClient() {
    timeoutSec := getEnvFloat("TIMEOUT", 5.0)
    config.Timeout = time.Duration(timeoutSec * float64(time.Second))

    proxyURL := os.Getenv("PROXY") // 例如 http://192.168.100.2:7890

    // 统一的 TLS 配置：忽略证书验证
    tlsConfig := &tls.Config{
        InsecureSkipVerify: true,
    }

    var transport http.RoundTripper
    if proxyURL != "" {
        proxyURLParsed, err := url.Parse(proxyURL)
        if err == nil {
            if proxyURLParsed.Scheme == "socks5" {
                dialer, err := proxy.SOCKS5("tcp", proxyURLParsed.Host, nil, proxy.Direct)
                if err == nil {
                    transport = &http.Transport{
                        Dial:           dialer.Dial,
                        TLSClientConfig: tlsConfig,   // 添加
                    }
                } else {
                    log.Printf("Failed to create SOCKS5 dialer: %v", err)
                }
            } else {
                // http/https 代理
                transport = &http.Transport{
                    Proxy:           http.ProxyURL(proxyURLParsed),
                    TLSClientConfig: tlsConfig,       // 添加
                }
            }
        }
    }
    if transport == nil {
        transport = &http.Transport{
            MaxIdleConns:    100,
            IdleConnTimeout: 90 * time.Second,
            TLSClientConfig: tlsConfig,               // 添加
        }
    }

    httpClient = &http.Client{
        Timeout:   config.Timeout,
        Transport: transport,
        CheckRedirect: func(req *http.Request, via []*http.Request) error {
            return http.ErrUseLastResponse // 不跟随重定向
        },
    }
}

// ---------- 响应结构 ----------
type SingleResult struct {
	URL    string `json:"url"`
	Status string `json:"status"` // "normal" or "abnormal"
	Code   int    `json:"code,omitempty"`
	Error  string `json:"error,omitempty"`
}

type BatchResult struct {
	Total         int           `json:"total"`
	NormalCount   int           `json:"normal_count"`
	AbnormalCount int           `json:"abnormal_count"`
	Details       []SingleResult `json:"details"`
}

// ---------- 单 URL 检测 ----------
func checkSingleURL(targetURL string) SingleResult {
	start := time.Now()

	req, err := http.NewRequest("GET", targetURL, nil)
	if err != nil {
		elapsed := time.Since(start)
		log.Printf("检测失败 | URL: %s | 耗时: %v | 错误: Invalid URL", targetURL, elapsed)
		return SingleResult{URL: targetURL, Status: "abnormal", Error: "Invalid URL"}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		errMsg := err.Error()
		if strings.Contains(errMsg, "timeout") {
			errMsg = "Request timeout"
		} else if strings.Contains(errMsg, "connection refused") || strings.Contains(errMsg, "no such host") {
			errMsg = "Connection error"
		} else {
			errMsg = "Request error: " + errMsg
		}
		elapsed := time.Since(start)
		log.Printf("检测失败 | URL: %s | 耗时: %v | 错误: %s", targetURL, elapsed, errMsg)
		return SingleResult{URL: targetURL, Status: "abnormal", Error: errMsg}
	}
	defer resp.Body.Close()

	code := resp.StatusCode
	_, normal := config.NormalStatusCodes[code]
	status := "normal"
	if !normal {
		status = "abnormal"
	}
	elapsed := time.Since(start)
	log.Printf("检测成功 | URL: %s | 耗时: %v | 状态码: %d | 判定: %s", targetURL, elapsed, code, status)
	return SingleResult{
		URL:    targetURL,
		Status: status,
		Code:   code,
	}
}

// ---------- 限流器 ----------
type Semaphore struct {
	ch chan struct{}
}

func NewSemaphore(max int) *Semaphore {
	return &Semaphore{ch: make(chan struct{}, max)}
}
func (s *Semaphore) Acquire() { s.ch <- struct{}{} }
func (s *Semaphore) Release() { <-s.ch }

// ---------- 批量检测 ----------
func batchCheck(urls []string) BatchResult {
	// 去重保序
	seen := make(map[string]bool)
	unique := make([]string, 0, len(urls))
	for _, u := range urls {
		if !seen[u] {
			seen[u] = true
			unique = append(unique, u)
		}
	}

	sem := NewSemaphore(config.MaxConcurrent)
	var wg sync.WaitGroup
	results := make([]SingleResult, len(unique))
	for i, u := range unique {
		wg.Add(1)
		go func(idx int, urlStr string) {
			defer wg.Done()
			sem.Acquire()
			defer sem.Release()
			results[idx] = checkSingleURL(urlStr)
		}(i, u)
	}
	wg.Wait()

	normalCount := 0
	abnormalCount := 0
	for _, r := range results {
		if r.Status == "normal" {
			normalCount++
		} else {
			abnormalCount++
		}
	}
	return BatchResult{
		Total:         len(results),
		NormalCount:   normalCount,
		AbnormalCount: abnormalCount,
		Details:       results,
	}
}

// ---------- HTTP 处理函数 ----------
func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"status":"ok"}`))
}

func singleStatusHandler(w http.ResponseWriter, r *http.Request) {
	rawURL := r.URL.Query().Get("url")
	if rawURL == "" {
		http.Error(w, "Missing 'url' parameter", http.StatusBadRequest)
		return
	}
	result := checkSingleURL(rawURL)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func batchStatusHandler(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Content-Type") != "application/json" {
		http.Error(w, "Content-Type must be application/json", http.StatusBadRequest)
		return
	}
	var urls []string
	if err := json.NewDecoder(r.Body).Decode(&urls); err != nil {
		http.Error(w, "Invalid JSON body", http.StatusBadRequest)
		return
	}
	if len(urls) == 0 {
		http.Error(w, "URL list cannot be empty", http.StatusBadRequest)
		return
	}
	if len(urls) > config.MaxBatchSize {
		http.Error(w, fmt.Sprintf("Batch size exceeds limit: %d", config.MaxBatchSize), http.StatusBadRequest)
		return
	}
	result := batchCheck(urls)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

// ---------- 主函数 ----------
func main() {
	// 加载配置
	config.NormalStatusCodes = parseStatusCodes("NORMAL_STATUS_CODES", []int{200, 302, 307, 401, 403})
	config.MaxBatchSize = getEnvInt("MAX_BATCH_SIZE", 50)
	config.MaxConcurrent = getEnvInt("MAX_CONCURRENT", 10)
	initHTTPClient()

	// 路由
	http.HandleFunc("/", healthHandler)
	http.HandleFunc("/health", healthHandler)
	http.HandleFunc("/status", singleStatusHandler)
	http.HandleFunc("/status/batch", batchStatusHandler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8000"
	}
	log.Printf("Server starting on :%s", port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatal(err)
	}
}
