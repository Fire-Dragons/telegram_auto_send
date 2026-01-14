# 基础镜像：Python 3.9 官方镜像（轻量、稳定）
FROM python:3.9-slim

# 维护者信息
LABEL maintainer="your-name <your-email@xxx.com>"

# 设置工作目录
WORKDIR /app

# 创建非 root 用户（安全要求）
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 安装系统依赖（python-magic 需 libmagic）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装（先复制 requirements 利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建数据目录并设置权限
RUN mkdir -p /app/data/user_sessions /app/data/user_media /app/data/logs \
    && chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 暴露 Flask 端口
EXPOSE 5000

# 启动命令（前台运行，保证容器不退出）
CMD ["python", "app.py"]