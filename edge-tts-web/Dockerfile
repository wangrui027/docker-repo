FROM python:alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi uvicorn edge-tts

# 使用阿里云镜像加速
# RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ fastapi uvicorn edge-tts

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
