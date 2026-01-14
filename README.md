# Telegram Bot Docker 自动化部署 README

# Telegram Bot Docker 自动化部署

一个基于 Python 开发的 Telegram 机器人项目，通过 GitHub Actions 实现 Docker 镜像自动构建、推送，支持手动/自动触发构建，适配多架构部署。

## 🌟 项目特性

- 🐳 基于 Docker 容器化部署，环境隔离，一键启动

- 🤖 适配最新版 `python-telegram-bot`（v20.7），支持 Telegram Bot API 最新特性

- ⚡ GitHub Actions 自动化：代码推送/手动触发自动构建 Docker 镜像

- 📱 多架构支持：兼容 `linux/amd64`（x86 服务器）、`linux/arm64`（ARM 服务器/树莓派）

- 🔒 安全优化：非 root 用户运行容器，最小化镜像体积

## 📋 前置准备

### 1. 必要密钥/配置

**Telegram 相关**：

- `BOT_TOKEN`：从 [@BotFather](https://t.me/BotFather) 获取的机器人 Token

- `API_ID`/`API_HASH`：从 [my.telegram.org](https://my.telegram.org/) 获取的 Telegram 开发者密钥

**Docker Hub 相关**：

- `DOCKER_HUB_USERNAME`：Docker Hub 用户名

- `DOCKER_HUB_TOKEN`：Docker Hub 访问令牌（需开启 `Read/Write` 权限）

### 2. 环境要求

- 服务器：支持 Docker 的 Linux 服务器（Ubuntu/Debian/CentOS 均可）

- GitHub 账号：拥有仓库的 `Write` 权限（用于配置 Actions 密钥）

## 🚀 快速部署

### 方式 1：使用 GitHub Actions 构建的镜像（推荐）

#### 步骤 1：配置 GitHub 密钥

进入 GitHub 仓库 → `Settings` → `Secrets and variables` → `Actions`，添加以下密钥：

|密钥名称|说明|
|---|---|
|`DOCKER_HUB_USERNAME`|Docker Hub 用户名|
|`DOCKER_HUB_TOKEN`|Docker Hub 访问令牌|
|`BOT_TOKEN`（可选）|Telegram 机器人 Token（仅用于镜像测试）|
|`API_ID`（可选）|Telegram API ID（仅用于镜像测试）|
|`API_HASH`（可选）|Telegram API Hash（仅用于镜像测试）|
#### 步骤 2：触发镜像构建

- **自动触发**：推代码到 `main` 分支，Actions 自动构建并推送 `latest` 标签镜像

- **手动触发**：
        

    1. 仓库 → `Actions` → 选择 `Build and Push Docker Image`

    2. 点击 `Run workflow`，填写参数（镜像标签/构建架构）后触发

#### 步骤 3：服务器拉取并启动镜像

```bash

# 拉取镜像（替换为你的 Docker Hub 用户名）
docker pull 你的用户名/telegram-bot:latest

# 启动容器（持久化数据，自动重启）
docker run -d \
  --name telegram-bot \
  --restart always \
  -v /usr/local/telegram-bot/data:/app/data \
  -e BOT_TOKEN=你的机器人Token \
  -e API_ID=你的API_ID \
  -e API_HASH=你的API_HASH \
  -e DOMAIN=https://你的域名.com \
  -p 5000:5000 \
  你的用户名/telegram-bot:latest
```

### 方式 2：本地构建镜像

```bash

# 克隆仓库
git clone https://github.com/你的用户名/telegram-bot.git
cd telegram-bot

# 构建镜像
docker build -t telegram-bot:latest .

# 启动容器
docker run -d \
  --name telegram-bot \
  -v ./data:/app/data \
  -e BOT_TOKEN=你的机器人Token \
  -e API_ID=你的API_ID \
  -e API_HASH=你的API_HASH \
  -p 5000:5000 \
  telegram-bot:latest
```

## ⚙️ 配置说明

### 环境变量

|变量名|必选|说明|
|---|---|---|
|`BOT_TOKEN`|是|Telegram 机器人 Token|
|`API_ID`|是|Telegram 开发者 API ID|
|`API_HASH`|是|Telegram 开发者 API Hash|
|`DOMAIN`|否|机器人 Web 服务域名（用于登录页面）|
|`TZ`|否|时区（默认 `Asia/Shanghai`）|
### 数据持久化

容器内 `/app/data` 目录包含：

- `user_sessions`：用户会话数据

- `user_media`：媒体文件（图片/视频）

- `logs`：机器人运行日志

通过 `-v` 挂载该目录到宿主机，避免容器重启后数据丢失。

## 🛠️ 常见问题

### 1. GitHub Actions 手动触发无反应

- 检查账号权限：需拥有仓库 `Write` 及以上权限

- 禁用浏览器插件（AdBlock/uBlock 等），或使用无痕模式

- 检查分支保护规则：取消 `Restrict who can push to matching branches`

### 2. 镜像构建报错 `invalid reference format`

- 原因：Docker 镜像标签格式非法（包含多个冒号）

- 解决方案：参考 GitHub Actions 工作流中 `Set image tags` 步骤，确保标签格式为 `用户名/镜像名:标签`

### 3. 容器启动失败

- 查看日志：`docker logs telegram-bot`

- 检查密钥是否正确：`BOT_TOKEN`/`API_ID`/`API_HASH` 不能为空

- 检查端口占用：`netstat -tulpn | grep 5000`，更换未占用端口

### 4. Dockerfile 报 `FromAsCasing` 警告

- 原因：`FROM`/`as` 关键字大小写不统一

- 解决方案：严格遵循 `FROM python:3.11-slim as builder`（`FROM` 大写，`as` 小写）



## 📞 维护说明

- 镜像自动构建：推代码到 `main` 分支或手动触发 Actions 即可更新镜像

- 容器更新：先停止旧容器 → 拉取新镜像 → 启动新容器
`docker stop telegram-bot && docker rm telegram-bot
docker pull 你的用户名/telegram-bot:latest
# 重新执行启动命令`
> （注：文档部分内容可能由 AI 生成）