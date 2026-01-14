# 阶段1：构建依赖
FROM python:3.11-slim as builder

WORKDIR /app

# 安装依赖工具
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

# 阶段2：生成最终镜像
FROM python:3.11-slim

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

# 创建数据目录并设置权限
RUN mkdir -p /app/data/user_sessions /app/data/user_media /app/data/logs \
    && chmod -R 777 /app/data

# 暴露端口
EXPOSE 5000

# 启动命令（gunicorn最新版）
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]