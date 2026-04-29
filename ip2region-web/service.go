package main

import (
    "fmt"
    "sync"

    "github.com/lionsoul2014/ip2region/binding/golang/service"
)

var (
    ip2regionSvc *service.Ip2Region
    once         sync.Once
)

// InitIp2Region 初始化 ip2region 服务（单例）
func InitIp2Region(v4XdbPath string, cacheStrategy int) error {
    var err error
    once.Do(func() {
        // 配置缓存策略：service.BufferCache 为全内存缓存，性能最佳
        // 参数3: 初始查询器数量（BufferCache 模式下此参数无效）
        v4Config, e := service.NewV4Config(cacheStrategy, v4XdbPath, 20)
        if e != nil {
            err = fmt.Errorf("创建 v4 配置失败: %w", e)
            return
        }
        // v6 可不配置，传入 nil 表示不支持 IPv6 查询
        ip2regionSvc, e = service.NewIp2Region(v4Config, nil)
        if e != nil {
            err = fmt.Errorf("创建 ip2region 服务失败: %w", e)
        }
    })
    return err
}

// SearchIP 查询 IP 归属地
func SearchIP(ip string) (string, error) {
    if ip2regionSvc == nil {
        return "", fmt.Errorf("ip2region 服务未初始化")
    }
    return ip2regionSvc.Search(ip)
}

// CloseIp2Region 关闭服务，释放资源
func CloseIp2Region() {
    if ip2regionSvc != nil {
        ip2regionSvc.Close()
    }
}
