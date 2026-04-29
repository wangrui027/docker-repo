# 第一阶段：编译 Go 静态二进制
FROM golang:alpine AS builder

WORKDIR /app
COPY main.go .
RUN go mod init tts-server && \
    go mod tidy && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o tts-server main.go

# 第二阶段：基于 python:alpine，安装 edge-tts，复制二进制
FROM python:alpine

# 安装 edge-tts（需要 pip）
RUN pip install --no-cache-dir edge-tts

# 复制 Go 二进制
COPY --from=builder /app/tts-server /app/tts-server

# 暴露端口
EXPOSE 8000

# 运行 Go 服务
ENTRYPOINT ["/app/tts-server"]
