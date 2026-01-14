# 阶段1：构建依赖（严格遵循Dockerfile规范，消除FromAsCasing警告）
FROM python:3.11-slim as builder

WORKDIR /app

# 安装依赖工具（清理apt缓存，减小镜像体积）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# 配置国内PyPI源加速依赖安装
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 安装Python依赖（生成wheel包，减小最终镜像体积）
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

# 阶段2：生成最终镜像（同样严格规范FROM/as大小写）
FROM python:3.11-slim as final

WORKDIR /app

# 配置时区和基础依赖
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖包并安装
COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache /wheels/*

# 复制项目文件
COPY . .

# 创建数据目录并设置权限（规范目录权限）
RUN mkdir -p /app/data/user_sessions /app/data/user_media /app/data/logs \
    && chmod -R 755 /app/data \
    && chown -R 1000:1000 /app/data

# 暴露端口
EXPOSE 5000

# 切换非root用户（提升安全性，消除NonRoot警告）
USER 1000

# 启动命令（gunicorn最新版，规范参数）
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]