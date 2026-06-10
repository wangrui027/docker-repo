FROM python:alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# 安装依赖（提前复制 requirements.txt 以利用缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制所有源代码
COPY . .

# 暴露 Flask 默认端口（main.py 中配置为 5000）
EXPOSE 5000

# 启动命令
CMD ["python", "main.py"]
