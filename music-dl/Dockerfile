FROM alpine

ENV TZ=Asia/Shanghai

RUN mkdir -p /music-dl

WORKDIR /music-dl

# 版本号可随时修改
ARG MUSIC_DL_VERSION=v1.0.17
ENV MUSIC_DL_VERSION=${MUSIC_DL_VERSION}

RUN ARCH=$(uname -m) && \
    case $ARCH in \
        x86_64)  OS_ARCH="amd64" ;; \
        aarch64) OS_ARCH="arm64" ;; \
        arm64)   OS_ARCH="arm64" ;; \
        *)       echo "Unsupported architecture: $ARCH"; exit 1 ;; \
    esac && \
    wget --tries=3 --timeout=30 \
        "https://github.com/guohuiyuan/go-music-dl/releases/download/${MUSIC_DL_VERSION}/go-music-dl_linux_${OS_ARCH}.tar.gz" \
        -O /tmp/go-music-dl.tar.gz && \
    tar -xzf /tmp/go-music-dl.tar.gz -C . && \
    rm /tmp/go-music-dl.tar.gz

EXPOSE 8080

CMD ["/music-dl/music-dl", "web", "--port", "8080"]
