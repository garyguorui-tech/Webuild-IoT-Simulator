# 现场设备模拟器 / 采集器 公共镜像（fieldsim 与 collector 共用）
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt docker/requirements-extra.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-extra.txt

# 再拷源码（.dockerignore 已排除 .venv/docs/__pycache__ 等）
COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000 1502 1503 1504

# 默认作为设备群模拟器 + 面板运行；collector 服务在 compose 里覆盖 command
CMD ["python", "run_fleet.py", "--web-host", "0.0.0.0", "--web-port", "8000", "--modbus-bind", "0.0.0.0"]
