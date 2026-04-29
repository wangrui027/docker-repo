# 阶段一：构建器（使用 alpine）
FROM golang:alpine AS builder

# 配置 Go 模块代理
ENV GOPROXY=https://goproxy.cn,direct

WORKDIR /build
COPY . .

RUN go mod init ip2region-web && \
    go get github.com/lionsoul2014/ip2region/binding/golang && \
    go mod tidy

# 关键：CGO_ENABLED=0 生成静态二进制，不需要外部依赖
RUN CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o ip2query .

# 阶段二：最终镜像（使用空镜像 scratch）
FROM scratch

WORKDIR /app

# 从构建阶段复制编译好的二进制
COPY --from=builder /build/ip2query .

# 直接使用 ADD 从 GitHub 下载 xdb 文件
ADD https://github.com/lionsoul2014/ip2region/raw/refs/heads/master/data/ip2region_v4.xdb /app/data/ip2region_v4.xdb

EXPOSE 8080
ENTRYPOINT ["./ip2query", "-xdb=/app/data/ip2region_v4.xdb", "-cache=buffer"]
