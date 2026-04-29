# 多阶段构建，最终镜像仅包含静态二进制文件
FROM golang:alpine AS builder

WORKDIR /app
COPY main.go .
RUN go mod init http-checker && \
    go get golang.org/x/net/proxy && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o http-checker main.go

FROM scratch
COPY --from=builder /app/http-checker /http-checker
EXPOSE 8000
ENTRYPOINT ["/http-checker"]
