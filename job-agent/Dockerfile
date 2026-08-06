FROM python:3.12-slim

# 构建参数（代理 / 国内镜像加速）
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

COPY main.py .
COPY agent.py .
COPY config.py .
COPY config.yaml .
COPY database.py .
COPY engine.py .
COPY resume.py .
COPY resume_parser.py .
COPY cover_letter.py .
COPY interview.py .
COPY search.py .
COPY routes/ routes/
COPY static/ static/

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
