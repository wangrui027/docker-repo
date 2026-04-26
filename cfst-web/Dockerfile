FROM python:alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV CFST_PATH /cfst/cfst
ENV CFST_WORKDIR /cfst
ENV CFST_TIMEOUT 600

# 把依赖文件放前面，利用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有源代码
COPY . .

# 版本号可随时修改
ARG CFST_VERSION=v2.3.4
ENV CFST_VERSION=${CFST_VERSION}

# 使用 uname -m 自动检测架构并下载对应版本
RUN ARCH=$(uname -m) && \
    case $ARCH in \
        x86_64)  OS_ARCH="amd64" ;; \
        aarch64) OS_ARCH="arm64" ;; \
        arm64)   OS_ARCH="arm64" ;; \
        *)       echo "Unsupported architecture: $ARCH"; exit 1 ;; \
    esac && \
    wget --tries=3 --timeout=30 \
        "https://github.com/XIU2/CloudflareSpeedTest/releases/download/${CFST_VERSION}/cfst_linux_${OS_ARCH}.tar.gz" \
        -O /tmp/cfst.tar.gz && \
    mkdir -p /cfst && \
    tar -xzf /tmp/cfst.tar.gz -C /cfst && \
    rm /tmp/cfst.tar.gz

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
