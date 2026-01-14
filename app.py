import os
import json
import shutil
import time
import logging
import magic
import datetime
import glob
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters, CallbackQueryHandler
from pyrogram import Client, errors
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

# ======================== 初始化配置 ========================
load_dotenv()
# 基础配置
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_USERNAME = os.getenv("BOT_USERNAME")
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", 5000))
DOMAIN = os.getenv("DOMAIN")

# 安全配置
MESSAGE_LIMIT = int(os.getenv("MESSAGE_LIMIT", 5))          # 每分钟最多发送消息数
GROUP_MSG_LIMIT = int(os.getenv("GROUP_MSG_LIMIT", 20))     # 每天单群组最多发送消息数
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", 30))# 日志保留天数

# 目录配置（适配Docker挂载）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, "data", "user_sessions")
TASKS_FILE = os.path.join(BASE_DIR, "user_tasks.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
MEDIA_DIR = os.path.join(BASE_DIR, "data", "user_media")
LOG_FILE = os.path.join(BASE_DIR, "data", "logs", "operation.log")
BANNED_KEYWORDS_FILE = os.path.join(BASE_DIR, "banned_keywords.txt")

# 创建必要目录
os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# ======================== 全局状态管理 ========================
# 用户消息频率记录
user_message_records = {}
# 用户任务创建状态（按钮交互用）
user_task_state = {}  # {user_id: {"step": 步骤, "temp_data": 临时数据}}
# 用户任务数据
user_tasks = {}

# ======================== 安全合规核心配置 ========================
# 1. 日志配置（操作审计，不记录敏感内容）
logging.basicConfig(
    filename=LOG_FILE,
    format="%(asctime)s - user_id=%(user_id)s - operation=%(operation)s - result=%(result)s - detail=%(detail)s",
    level=logging.INFO,
    encoding="utf-8"
)

def log_operation(user_id, operation, result, detail=""):
    """记录用户操作日志"""
    extra = {
        'user_id': user_id,
        'operation': operation,
        'result': result,
        'detail': detail[:200]  # 限制详情长度
    }
    logging.info("", extra=extra)

# 2. 清理过期日志
def clean_expired_logs():
    """清理超过保留天数的日志"""
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=LOG_RETENTION_DAYS)
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if " - user_id=" not in line:
                    f.write(line)
                    continue
                log_time_str = line.split(" - ")[0]
                try:
                    log_time = datetime.datetime.strptime(log_time_str, "%Y-%m-%d %H:%M:%S,%f")
                    if log_time >= cutoff_date:
                        f.write(line)
                except:
                    f.write(line)
        log_operation("system", "clean_logs", "success", f"清理了{LOG_RETENTION_DAYS}天前的日志")
    except Exception as e:
        log_operation("system", "clean_logs", "failed", str(e))

# 3. 违规关键词加载
def load_banned_keywords():
    """加载违规关键词库"""
    if os.path.exists(BANNED_KEYWORDS_FILE):
        with open(BANNED_KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []
BANNED_KEYWORDS = load_banned_keywords()

# 4. 频率限制装饰器
def rate_limit(func):
    """消息发送频率限制"""
    @wraps(func)
    def wrapper(user_id, chat_id, *args, **kwargs):
        now = time.time()
        user_id_str = str(user_id)
        chat_id_str = str(chat_id)
        
        if user_id_str not in user_message_records:
            user_message_records[user_id_str] = {
                "last_time": now,
                "count": 0,
                "group_counts": {},
                "group_reset_time": now
            }
        
        user_record = user_message_records[user_id_str]
        
        # 重置每天的群组计数
        if now - user_record["group_reset_time"] > 86400:
            user_record["group_counts"] = {}
            user_record["group_reset_time"] = now
        
        # 每分钟消息数限制
        if now - user_record["last_time"] < 60:
            user_record["count"] += 1
            if user_record["count"] > MESSAGE_LIMIT:
                log_operation(user_id_str, "send_message", "failed", f"频率超限：每分钟最多{MESSAGE_LIMIT}条")
                return False, f"发送频率过高，请1分钟后再试（每分钟最多{MESSAGE_LIMIT}条）"
        else:
            user_record["count"] = 1
            user_record["last_time"] = now
        
        # 每天单群组消息数限制
        if chat_id_str not in user_record["group_counts"]:
            user_record["group_counts"][chat_id_str] = 0
        user_record["group_counts"][chat_id_str] += 1
        if user_record["group_counts"][chat_id_str] > GROUP_MSG_LIMIT:
            log_operation(user_id_str, "send_message", "failed", f"群组消息超限：每天单群组最多{GROUP_MSG_LIMIT}条")
            return False, f"向该群组发送消息过多，请明天再试（每天最多{GROUP_MSG_LIMIT}条）"
        
        return func(user_id, chat_id, *args, **kwargs)
    return wrapper

# 5. 内容风控
def check_content(content):
    """检查内容是否包含违规关键词"""
    if not content:
        return True, "内容合规"
    for keyword in BANNED_KEYWORDS:
        if keyword in content:
            return False, f"内容包含违规关键词：{keyword}"
    return True, "内容合规"

# 6. 文件权限设置
def set_file_permission(file_path):
    """设置文件权限为600"""
    try:
        os.chmod(file_path, 0o600)
        return True
    except:
        return False

# ======================== 数据存储函数 ========================
def load_user_tasks():
    """加载用户定时任务"""
    global user_tasks
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                user_tasks = json.load(f)
        except:
            user_tasks = {}
    else:
        user_tasks = {}

def save_user_tasks():
    """保存用户定时任务"""
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(user_tasks, f, ensure_ascii=False, indent=2)

# 初始化加载任务
load_user_tasks()

# ======================== 工具函数 ========================
def get_user_client(user_id):
    """获取Pyrogram客户端"""
    session_path = os.path.join(SESSION_DIR, f"user_{user_id}")
    client = Client(
        name=session_path,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=SESSION_DIR
    )
    return client

def get_user_media_dir(user_id):
    """获取用户媒体文件目录"""
    media_dir = os.path.join(MEDIA_DIR, f"user_{user_id}")
    os.makedirs(media_dir, exist_ok=True)
    return media_dir

def get_media_type(file_path):
    """识别媒体文件类型"""
    mime_type = magic.from_file(file_path, mime=True)
    if mime_type.startswith("image/"):
        return "photo"
    elif mime_type.startswith("video/"):
        return "video"
    else:
        return "document"

# ======================== 消息发送函数 ========================
@rate_limit
def send_text_message(user_id, chat_id, text, parse_mode="markdown"):
    """发送文本消息"""
    # 内容风控
    is_valid, msg = check_content(text)
    if not is_valid:
        log_operation(user_id, "send_text", "failed", f"内容违规：{msg}")
        return False, msg
    
    client = get_user_client(user_id)
    try:
        client.start()
        # 校验群组权限
        client.get_chat(chat_id)
        # 发送消息
        client.send_message(chat_id, text, parse_mode=parse_mode)
        client.stop()
        log_operation(user_id, "send_text", "success", f"发送到{chat_id}，内容长度：{len(text)}")
        return True, "文本消息发送成功"
    except errors.ChatNotFound:
        client.stop()
        log_operation(user_id, "send_text", "failed", f"群组/用户不存在：{chat_id}")
        return False, "无法发送：群组/用户不存在或你未加入该群组"
    except Exception as e:
        client.stop()
        log_operation(user_id, "send_text", "failed", str(e))
        return False, f"文本发送失败：{str(e)}"

@rate_limit
def send_media_message(user_id, chat_id, media_path, caption="", parse_mode="markdown"):
    """发送媒体消息"""
    # 内容风控
    is_valid, msg = check_content(caption)
    if not is_valid:
        log_operation(user_id, "send_media", "failed", f"说明文字违规：{msg}")
        return False, msg
    
    # 过滤可执行文件
    banned_ext = [".exe", ".bat", ".sh", ".py", ".js"]
    file_ext = os.path.splitext(media_path)[1].lower()
    if file_ext in banned_ext:
        log_operation(user_id, "send_media", "failed", f"禁止发送可执行文件：{file_ext}")
        return False, "禁止发送可执行文件（exe/bat/sh等）"
    
    client = get_user_client(user_id)
    try:
        client.start()
        # 校验群组权限
        client.get_chat(chat_id)
        # 发送媒体
        media_type = get_media_type(media_path)
        if media_type == "photo":
            client.send_photo(chat_id, media_path, caption=caption, parse_mode=parse_mode)
        elif media_type == "video":
            client.send_video(chat_id, media_path, caption=caption, parse_mode=parse_mode)
        else:
            client.send_document(chat_id, media_path, caption=caption, parse_mode=parse_mode)
        client.stop()
        log_operation(user_id, "send_media", "success", f"发送到{chat_id}，文件：{os.path.basename(media_path)}")
        return True, "媒体消息发送成功"
    except errors.ChatNotFound:
        client.stop()
        log_operation(user_id, "send_media", "failed", f"群组/用户不存在：{chat_id}")
        return False, "无法发送：群组/用户不存在或你未加入该群组"
    except Exception as e:
        client.stop()
        log_operation(user_id, "send_media", "failed", str(e))
        return False, f"媒体发送失败：{str(e)}"

def send_checkin_message(user_id, chat_id, checkin_cmd):
    """发送签到指令"""
    sensitive_cmds = ["/kick", "/ban", "/mute", "/unban", "/promote"]
    if any(cmd in checkin_cmd for cmd in sensitive_cmds):
        log_operation(user_id, "send_checkin", "failed", f"敏感指令：{checkin_cmd}")
        return False, "禁止发送群组管理类敏感指令"
    return send_text_message(user_id, chat_id, checkin_cmd)

# ======================== 定时任务执行函数 ========================
def execute_task(task_id):
    """执行定时任务"""
    task_info = None
    user_id = None
    for uid, tasks in user_tasks.items():
        if task_id in tasks:
            user_id = uid
            task_info = tasks[task_id]
            break
    
    if not task_info:
        log_operation("system", "execute_task", "failed", f"任务不存在：{task_id}")
        return
    
    chat_id = task_info.get("chat_id")
    task_type = task_info.get("type", "text")
    
    try:
        if task_type == "checkin":
            checkin_cmd = task_info["checkin_cmd"]
            success, msg = send_checkin_message(user_id, chat_id, checkin_cmd)
        elif task_type == "media":
            media_path = task_info["media_path"]
            caption = task_info.get("caption", "")
            success, msg = send_media_message(user_id, chat_id, media_path, caption)
        else:
            text = task_info["text"]
            success, msg = send_text_message(user_id, chat_id, text)
        
        log_operation(user_id, "execute_task", "success" if success else "failed", 
                      f"任务ID：{task_id}，类型：{task_type}，结果：{msg}")
    except Exception as e:
        log_operation(user_id, "execute_task", "failed", f"任务ID：{task_id}，异常：{str(e)}")

# ======================== 按钮菜单构建（多级周期） ========================
def build_main_menu():
    """构建主功能按钮菜单"""
    keyboard = [
        [InlineKeyboardButton("📝 添加文本任务", callback_data="add_text_task")],
        [InlineKeyboardButton("🔄 添加签到任务", callback_data="add_checkin_task")],
        [InlineKeyboardButton("🖼️ 添加媒体任务", callback_data="add_media_task")],
        [InlineKeyboardButton("📋 查看所有任务", callback_data="list_tasks")],
        [InlineKeyboardButton("🗑️ 删除任务", callback_data="delete_task")],
        [InlineKeyboardButton("🚫 删除所有数据", callback_data="delete_all")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_trigger_menu():
    """构建周期选择一级菜单（大类）"""
    keyboard = [
        [InlineKeyboardButton("⏱️ 一次性任务", callback_data="trigger_date")],
        [InlineKeyboardButton("📅 间隔重复", callback_data="trigger_interval_menu")],
        [InlineKeyboardButton("📆 日历规则", callback_data="trigger_cron_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_interval_submenu():
    """间隔重复二级菜单"""
    keyboard = [
        [InlineKeyboardButton("每分钟重复", callback_data="interval_minute")],
        [InlineKeyboardButton("每小时重复", callback_data="interval_hour")],
        [InlineKeyboardButton("每天重复", callback_data="interval_day")],
        [InlineKeyboardButton("每2天重复", callback_data="interval_2day")],
        [InlineKeyboardButton("每周重复", callback_data="interval_week")],
        [InlineKeyboardButton("🔙 返回上一级", callback_data="back_to_trigger")]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_cron_submenu():
    """日历规则二级菜单"""
    keyboard = [
        [InlineKeyboardButton("每天08:00执行", callback_data="cron_daily_0800")],
        [InlineKeyboardButton("每周一三五18:00", callback_data="cron_week135_1800")],
        [InlineKeyboardButton("每月1号00:00", callback_data="cron_month1_0000")],
        [InlineKeyboardButton("工作日09:00执行", callback_data="cron_workday_0900")],
        [InlineKeyboardButton("周末10:00执行", callback_data="cron_weekend_1000")],
        [InlineKeyboardButton("🔙 返回上一级", callback_data="back_to_trigger")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ======================== Telegram机器人处理器（多级周期） ========================
def start(update: Update, context: CallbackContext):
    """启动命令，显示按钮菜单"""
    user_id = str(update.effective_user.id)
    if user_id not in user_tasks:
        user_tasks[user_id] = {}
        save_user_tasks()
    
    session_file = os.path.join(SESSION_DIR, f"user_{user_id}.session")
    if os.path.exists(session_file):
        reply_text = "👋 欢迎回来！请选择你要执行的操作："
        update.message.reply_text(reply_text, reply_markup=build_main_menu())
    else:
        reply_text = (
            "👋 欢迎使用定时消息/签到机器人！\n"
            "请先完成账号授权（仅存储session文件，不收集敏感信息）：\n"
            f"{DOMAIN}/login?user_id={user_id}"
        )
        update.message.reply_text(reply_text)
    log_operation(user_id, "start", "success", "发送欢迎消息+按钮菜单")

def button_callback(update: Update, context: CallbackContext):
    """处理按钮回调事件（含多级周期菜单）"""
    query = update.callback_query
    query.answer()  # 必须调用，否则按钮会一直转圈
    user_id = str(query.from_user.id)
    callback_data = query.data

    # ===== 主菜单回调 =====
    if callback_data == "list_tasks":
        list_tasks(update, context)
    elif callback_data == "delete_all":
        delete_all(update, context)
    elif callback_data in ["add_text_task", "add_checkin_task", "add_media_task"]:
        # 选择任务类型，进入周期选择一级菜单
        user_task_state[user_id] = {
            "step": "select_trigger",
            "temp_data": {"task_type": callback_data.split("_")[1]}  # text/checkin/media
        }
        query.edit_message_text("请选择任务重复周期：", reply_markup=build_trigger_menu())
    elif callback_data == "delete_task":
        query.edit_message_text("请回复你要删除的 **任务ID**：")
        user_task_state[user_id] = {"step": "input_delete_task_id"}

    # ===== 周期选择一级菜单回调 =====
    elif callback_data == "trigger_date":
        # 一次性任务
        temp_data = user_task_state[user_id]["temp_data"]
        temp_data["trigger_type"] = "date"
        temp_data["trigger_args"] = {}
        user_task_state[user_id]["step"] = "input_time"
        user_task_state[user_id]["temp_data"] = temp_data
        query.edit_message_text("请回复 **任务执行时间**（格式：YYYY-MM-DD HH:MM）：", parse_mode="markdown")
    elif callback_data == "trigger_interval_menu":
        # 进入间隔重复二级菜单
        query.edit_message_text("请选择间隔重复周期：", reply_markup=build_interval_submenu())
    elif callback_data == "trigger_cron_menu":
        # 进入日历规则二级菜单
        query.edit_message_text("请选择日历规则周期：", reply_markup=build_cron_submenu())
    elif callback_data == "back_to_trigger":
        # 返回周期选择一级菜单
        query.edit_message_text("请选择任务重复周期：", reply_markup=build_trigger_menu())

    # ===== 间隔重复二级菜单回调 =====
    elif callback_data.startswith("interval_"):
        temp_data = user_task_state[user_id]["temp_data"]
        temp_data["trigger_type"] = callback_data
        
        # 设置间隔重复参数
        if callback_data == "interval_minute":
            temp_data["trigger_args"] = {"seconds": 60}
            prompt = "请回复 **首次执行时间**（格式：YYYY-MM-DD HH:MM）："
        elif callback_data == "interval_hour":
            temp_data["trigger_args"] = {"hours": 1}
            prompt = "请回复 **首次执行时间**（格式：YYYY-MM-DD HH:MM）："
        elif callback_data == "interval_day":
            temp_data["trigger_args"] = {"days": 1}
            prompt = "请回复 **首次执行时间**（格式：YYYY-MM-DD HH:MM）："
        elif callback_data == "interval_2day":
            temp_data["trigger_args"] = {"days": 2}
            prompt = "请回复 **首次执行时间**（格式：YYYY-MM-DD HH:MM）："
        elif callback_data == "interval_week":
            temp_data["trigger_args"] = {"weeks": 1}
            prompt = "请回复 **首次执行时间**（格式：YYYY-MM-DD HH:MM）："
        
        user_task_state[user_id]["step"] = "input_time"
        user_task_state[user_id]["temp_data"] = temp_data
        query.edit_message_text(prompt, parse_mode="markdown")

    # ===== 日历规则二级菜单回调 =====
    elif callback_data.startswith("cron_"):
        temp_data = user_task_state[user_id]["temp_data"]
        temp_data["trigger_type"] = callback_data
        
        # 设置日历规则参数（时区默认Asia/Shanghai）
        if callback_data == "cron_daily_0800":
            temp_data["trigger_args"] = {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}
            prompt = "请回复 **首次执行日期**（格式：YYYY-MM-DD）："
        elif callback_data == "cron_week135_1800":
            temp_data["trigger_args"] = {"day_of_week": "1,3,5", "hour": 18, "minute": 0, "timezone": "Asia/Shanghai"}
            prompt = "请回复 **首次执行日期**（格式：YYYY-MM-DD）："
        elif callback_data == "cron_month1_0000":
            temp_data["trigger_args"] = {"day": 1, "hour": 0, "minute": 0, "timezone": "Asia/Shanghai"}
            prompt = "请回复 **首次执行年份月份**（格式：YYYY-MM）："
        elif callback_data == "cron_workday_0900":
            temp_data["trigger_args"] = {"day_of_week": "1-5", "hour": 9, "minute": 0, "timezone": "Asia/Shanghai"}
            prompt = "请回复 **首次执行日期**（格式：YYYY-MM-DD）："
        elif callback_data == "cron_weekend_1000":
            temp_data["trigger_args"] = {"day_of_week": "6,0", "hour": 10, "minute": 0, "timezone": "Asia/Shanghai"}
            prompt = "请回复 **首次执行日期**（格式：YYYY-MM-DD）："
        
        user_task_state[user_id]["step"] = "input_time"
        user_task_state[user_id]["temp_data"] = temp_data
        query.edit_message_text(prompt, parse_mode="markdown")

def handle_user_input(update: Update, context: CallbackContext):
    """处理用户输入的任务参数（适配多级周期时间格式）"""
    user_id = str(update.effective_user.id)
    if user_id not in user_task_state:
        update.message.reply_text("请先点击按钮选择操作！", reply_markup=build_main_menu())
        return

    step = user_task_state[user_id]["step"]
    temp_data = user_task_state[user_id]["temp_data"]
    input_text = update.message.text.strip()

    # ===== 步骤1：输入时间（适配不同周期的时间格式）=====
    if step == "input_time":
        try:
            trigger_type = temp_data["trigger_type"]
            # 处理一次性/间隔重复（需要完整时间 YYYY-MM-DD HH:MM）
            if trigger_type in ["date"] or trigger_type.startswith("interval_"):
                task_time = datetime.datetime.strptime(input_text, "%Y-%m-%d %H:%M")
                temp_data["start_time"] = task_time.strftime("%Y-%m-%d %H:%M")
            # 处理日历规则-每月1号（仅需要 YYYY-MM）
            elif trigger_type == "cron_month1_0000":
                task_time = datetime.datetime.strptime(input_text, "%Y-%m")
                temp_data["start_time"] = task_time.strftime("%Y-%m")
            # 处理其他日历规则（仅需要 YYYY-MM-DD）
            elif trigger_type.startswith("cron_"):
                task_time = datetime.datetime.strptime(input_text, "%Y-%m-%d")
                temp_data["start_time"] = task_time.strftime("%Y-%m-%d")
            
            # 根据任务类型提示输入下一个参数
            task_type = temp_data["task_type"]
            if task_type == "text":
                prompt = "请回复 **文本内容**（支持Markdown：*加粗*、[链接](url)）："
                next_step = "input_text_content"
            elif task_type == "checkin":
                prompt = "请回复 **群组ID + 签到指令**（示例：-123456789 /签到）："
                next_step = "input_checkin_info"
            elif task_type == "media":
                prompt = "请回复 **群组ID + 媒体文件名 + 说明**（示例：-123456789 pic1.jpg 今日福利）："
                next_step = "input_media_info"
            
            user_task_state[user_id]["step"] = next_step
            user_task_state[user_id]["temp_data"] = temp_data
            update.message.reply_text(prompt, parse_mode="markdown")
        except ValueError as e:
            # 针对性的时间格式错误提示
            trigger_type = temp_data["trigger_type"]
            if trigger_type == "cron_month1_0000":
                err_msg = "时间格式错误！请输入年份月份（格式：YYYY-MM），如 2026-01"
            elif trigger_type.startswith("cron_"):
                err_msg = "时间格式错误！请输入日期（格式：YYYY-MM-DD），如 2026-01-20"
            else:
                err_msg = "时间格式错误！请输入完整时间（格式：YYYY-MM-DD HH:MM），如 2026-01-20 08:00"
            update.message.reply_text(err_msg)
    
    # ===== 步骤2：输入文本内容 =====
    elif step == "input_text_content":
        try:
            temp_data["content"] = input_text
            temp_data["chat_id"] = str(update.effective_chat.id)
            create_scheduled_task(user_id, temp_data)
            del user_task_state[user_id]
            update.message.reply_text("✅ 文本任务添加成功！", reply_markup=build_main_menu())
        except Exception as e:
            update.message.reply_text(f"❌ 任务创建失败：{str(e)}")
    
    # ===== 步骤3：输入签到信息 =====
    elif step == "input_checkin_info":
        try:
            chat_id, checkin_cmd = input_text.split(" ", 1)
            temp_data["chat_id"] = chat_id.strip()
            temp_data["checkin_cmd"] = checkin_cmd.strip()
            create_scheduled_task(user_id, temp_data)
            del user_task_state[user_id]
            update.message.reply_text("✅ 签到任务添加成功！", reply_markup=build_main_menu())
        except ValueError:
            update.message.reply_text("格式错误！请回复：群组ID 签到指令")
    
    # ===== 步骤4：输入媒体信息 =====
    elif step == "input_media_info":
        try:
            parts = input_text.split(" ", 2)
            chat_id = parts[0].strip()
            media_filename = parts[1].strip()
            caption = parts[2].strip() if len(parts)>=3 else ""
            
            media_dir = get_user_media_dir(user_id)
            media_path = os.path.join(media_dir, media_filename)
            if not os.path.exists(media_path):
                update.message.reply_text("❌ 媒体文件不存在！")
                return
            
            temp_data["chat_id"] = chat_id
            temp_data["media_path"] = media_path
            temp_data["caption"] = caption
            create_scheduled_task(user_id, temp_data)
            del user_task_state[user_id]
            update.message.reply_text("✅ 媒体任务添加成功！", reply_markup=build_main_menu())
        except ValueError:
            update.message.reply_text("格式错误！请回复：群组ID 媒体文件名 说明")
    
    # ===== 步骤5：输入删除任务ID =====
    elif step == "input_delete_task_id":
        task_id = input_text.strip()
        if user_id not in user_tasks or task_id not in user_tasks[user_id]:
            update.message.reply_text("❌ 任务不存在或无权限！", reply_markup=build_main_menu())
        else:
            try:
                scheduler.remove_job(task_id)
                del user_tasks[user_id][task_id]
                save_user_tasks()
                update.message.reply_text(f"✅ 任务 {task_id} 已删除！", reply_markup=build_main_menu())
            except JobLookupError:
                del user_tasks[user_id][task_id]
                save_user_tasks()
                update.message.reply_text(f"✅ 任务 {task_id} 记录已删除！", reply_markup=build_main_menu())
        if user_id in user_task_state:
            del user_task_state[user_id]

def create_scheduled_task(user_id, temp_data):
    """创建定时任务（适配所有周期类型）"""
    task_type = temp_data["task_type"]
    trigger_type = temp_data["trigger_type"]
    trigger_args = temp_data["trigger_args"]
    start_time_str = temp_data["start_time"]

    # 生成任务ID
    task_id = f"{task_type}_{user_id}_{int(time.time())}"

    # 构建 APScheduler 触发器
    try:
        if trigger_type == "date":
            # 一次性任务
            start_time = datetime.datetime.strptime(start_time_str, "%Y-%m-%d %H:%M")
            trigger = start_time
        elif trigger_type.startswith("interval_"):
            # 间隔重复任务
            start_time = datetime.datetime.strptime(start_time_str, "%Y-%m-%d %H:%M")
            trigger = IntervalTrigger(start_date=start_time,** trigger_args, coalesce=True)  # 合并重叠任务
        elif trigger_type.startswith("cron_"):
            # 日历规则任务
            if trigger_type == "cron_month1_0000":
                # 每月1号：拼接完整时间
                start_time = datetime.datetime.strptime(start_time_str + "-01 00:00", "%Y-%m-%d %H:%M")
            else:
                # 其他日历规则：拼接默认时间（00:00）
                start_time = datetime.datetime.strptime(start_time_str + " 00:00", "%Y-%m-%d %H:%M")
            trigger = CronTrigger(start_date=start_time, **trigger_args)
        else:
            raise ValueError(f"不支持的周期类型：{trigger_type}")

        # 添加任务到调度器
        scheduler.add_job(
            execute_task,
            trigger=trigger,
            args=[task_id],
            id=task_id,
            replace_existing=True,
            misfire_grace_time=300  # 任务错过执行后，允许延迟5分钟执行
        )

        # 保存任务信息到 JSON
        task_info = {
            "type": task_type,
            "trigger_type": trigger_type,
            "trigger_args": trigger_args,
            "start_time": start_time_str,
            "chat_id": temp_data["chat_id"]
        }
        # 补充任务类型相关字段
        if task_type == "text":
            task_info["text"] = temp_data["content"]
        elif task_type == "checkin":
            task_info["checkin_cmd"] = temp_data["checkin_cmd"]
        elif task_type == "media":
            task_info["media_path"] = temp_data["media_path"]
            task_info["caption"] = temp_data["caption"]
        
        # 初始化用户任务字典
        if user_id not in user_tasks:
            user_tasks[user_id] = {}
        user_tasks[user_id][task_id] = task_info
        save_user_tasks()
        log_operation(user_id, "create_task", "success", f"任务ID：{task_id}，周期：{trigger_type}")
    except Exception as e:
        log_operation(user_id, "create_task", "failed", f"创建任务失败：{str(e)}")
        raise e

def list_tasks(update: Update, context: CallbackContext):
    """查看所有任务（优化周期描述）"""
    user_id = str(update.effective_user.id)
    if user_id not in user_tasks or not user_tasks[user_id]:
        update.message.reply_text("📄 你还没有添加任何任务！")
        log_operation(user_id, "list_tasks", "success", "无任务")
        return

    # 周期类型描述映射
    trigger_desc_map = {
        "date": "一次性",
        "interval_minute": "每分钟重复",
        "interval_hour": "每小时重复",
        "interval_day": "每天重复",
        "interval_2day": "每2天重复",
        "interval_week": "每周重复",
        "cron_daily_0800": "每天08:00执行",
        "cron_week135_1800": "每周一三五18:00",
        "cron_month1_0000": "每月1号00:00",
        "cron_workday_0900": "工作日09:00执行",
        "cron_weekend_1000": "周末10:00执行"
    }

    task_list = []
    for task_id, task_info in user_tasks[user_id].items():
        task_type = task_info.get("type", "text")
        trigger_type = task_info.get("trigger_type", "date")
        start_time = task_info.get("start_time", "未知")
        trigger_desc = trigger_desc_map.get(trigger_type, "未知周期")

        if task_type == "checkin":
            task_desc = (
                f"🆔 {task_id}（签到-{trigger_desc}）\n"
                f"⏰ 首次执行：{start_time}\n"
                f"👥 群组：{task_info['chat_id']}\n"
                f"📝 指令：{task_info['checkin_cmd']}\n"
                "---"
            )
        elif task_type == "media":
            task_desc = (
                f"🆔 {task_id}（媒体-{trigger_desc}）\n"
                f"⏰ 首次执行：{start_time}\n"
                f"👥 群组：{task_info['chat_id']}\n"
                f"🖼️ 文件：{os.path.basename(task_info['media_path'])}\n"
                "---"
            )
        else:
            task_desc = (
                f"🆔 {task_id}（文本-{trigger_desc}）\n"
                f"⏰ 首次执行：{start_time}\n"
                f"👥 发送到：{task_info['chat_id']}\n"
                f"📝 内容：{task_info['text'][:50]}...\n"
                "---"
            )
        task_list.append(task_desc)
    
    update.message.reply_text("📋 你的所有任务：\n" + "\n".join(task_list))
    log_operation(user_id, "list_tasks", "success", f"查看{len(task_list)}个任务")

def delete_all(update: Update, context: CallbackContext):
    """删除所有数据"""
    user_id = str(update.effective_user.id)
    try:
        # 删除session文件
        session_file = os.path.join(SESSION_DIR, f"user_{user_id}.session")
        if os.path.exists(session_file):
            os.remove(session_file)
        
        # 删除媒体文件
        media_dir = get_user_media_dir(user_id)
        if os.path.exists(media_dir):
            shutil.rmtree(media_dir)
        
        # 删除任务
        if user_id in user_tasks:
            for task_id in user_tasks[user_id]:
                try:
                    scheduler.remove_job(task_id)
                except:
                    pass
            del user_tasks[user_id]
            save_user_tasks()
        
        update.message.reply_text("✅ 你的所有数据已删除，不可恢复！", reply_markup=build_main_menu())
        log_operation(user_id, "delete_all", "success", "删除所有数据")
    except Exception as e:
        update.message.reply_text(f"❌ 数据删除失败：{str(e)}")
        log_operation(user_id, "delete_all", "failed", str(e))

def handle_media_upload(update: Update, context: CallbackContext):
    """处理媒体文件上传"""
    user_id = str(update.effective_user.id)
    media_dir = get_user_media_dir(user_id)
    
    try:
        if update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            file = context.bot.get_file(file_id)
            filename = f"photo_{int(time.time())}.jpg"
            file_path = os.path.join(media_dir, filename)
            file.download(file_path)
            update.message.reply_text(f"✅ 图片上传成功！\n文件ID：{filename}\n可用于媒体任务")
            log_operation(user_id, "upload_media", "success", f"上传图片：{filename}")
        
        elif update.message.video:
            video = update.message.video
            file_id = video.file_id
            file = context.bot.get_file(file_id)
            filename = f"video_{int(time.time())}.mp4"
            file_path = os.path.join(media_dir, filename)
            file.download(file_path)
            update.message.reply_text(f"✅ 视频上传成功！\n文件ID：{filename}\n可用于媒体任务")
            log_operation(user_id, "upload_media", "success", f"上传视频：{filename}")
        
        elif update.message.document:
            doc = update.message.document
            file_id = doc.file_id
            file = context.bot.get_file(file_id)
            filename = doc.file_name or f"doc_{int(time.time())}.bin"
            banned_ext = [".exe", ".bat", ".sh", ".py", ".js"]
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext in banned_ext:
                update.message.reply_text(f"❌ 禁止上传可执行文件：{file_ext}")
                log_operation(user_id, "upload_media", "failed", f"禁止上传可执行文件：{filename}")
                return
            
            file_path = os.path.join(media_dir, filename)
            file.download(file_path)
            update.message.reply_text(f"✅ 文档上传成功！\n文件ID：{filename}\n可用于媒体任务")
            log_operation(user_id, "upload_media", "success", f"上传文档：{filename}")
    except Exception as e:
        update.message.reply_text(f"❌ 媒体上传失败：{str(e)}")
        log_operation(user_id, "upload_media", "failed", str(e))

# ======================== Flask Web服务 ========================
app = Flask(__name__, static_folder=STATIC_DIR)

@app.route('/login')
def login_page():
    """登录页面"""
    user_id = request.args.get('user_id', '')
    return render_template(
        'login.html',
        bot_username=BOT_USERNAME,
        api_id=API_ID,
        api_hash=API_HASH,
        user_id=user_id
    )

@app.route('/privacy')
def privacy_page():
    """隐私政策"""
    return send_from_directory(STATIC_DIR, 'privacy.html')

@app.route('/auth')
def telegram_auth():
    """授权回调"""
    user_id = request.args.get('id', '')
    log_operation(user_id, "telegram_auth", "success", "扫码授权成功")
    return redirect(url_for('login_page', user_id=user_id))

@app.route('/upload_session', methods=['POST'])
def upload_session():
    """上传Session文件"""
    try:
        user_id = request.form.get('user_id')
        session_file = request.files.get('session_file')
        
        if not user_id or not session_file:
            return jsonify({"success": False, "message": "缺少参数"})
        
        if not session_file.filename.endswith('.session'):
            return jsonify({"success": False, "message": "请上传.session文件"})
        
        save_path = os.path.join(SESSION_DIR, f"user_{user_id}.session")
        session_file.save(save_path)
        set_file_permission(save_path)
        
        log_operation(user_id, "upload_session", "success", f"上传session文件：{session_file.filename}")
        return jsonify({"success": True, "message": "Session文件上传成功"})
    except Exception as e:
        log_operation(request.form.get('user_id', 'unknown'), "upload_session", "failed", str(e))
        return jsonify({"success": False, "message": str(e)})

@app.route('/upload_media', methods=['POST'])
def upload_media():
    """Web端上传媒体"""
    try:
        user_id = request.form.get('user_id')
        media_file = request.files.get('media_file')
        
        if not user_id or not media_file:
            return jsonify({"success": False, "message": "缺少参数"})
        
        banned_ext = [".exe", ".bat", ".sh", ".py", ".js"]
        file_ext = os.path.splitext(media_file.filename)[1].lower()
        if file_ext in banned_ext:
            return jsonify({"success": False, "message": "禁止上传可执行文件"})
        
        media_dir = get_user_media_dir(user_id)
        filename = media_file.filename
        save_path = os.path.join(media_dir, filename)
        media_file.save(save_path)
        
        log_operation(user_id, "web_upload_media", "success", f"上传媒体：{filename}")
        return jsonify({
            "success": True,
            "message": "媒体文件上传成功",
            "filename": filename
        })
    except Exception as e:
        log_operation(request.form.get('user_id', 'unknown'), "web_upload_media", "failed", str(e))
        return jsonify({"success": False, "message": str(e)})

# ======================== 主程序启动 ========================
if __name__ == "__main__":
    # 初始化调度器
    scheduler = BackgroundScheduler()
    scheduler.add_job(clean_expired_logs, 'cron', hour=0, minute=0)
    scheduler.start()
    print("⏰ APScheduler 定时任务调度器已启动")

    # 初始化Telegram Bot
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher
    
    # 注册处理器
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(button_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_user_input))
    dp.add_handler(MessageHandler(Filters.photo | Filters.video | Filters.document, handle_media_upload))
    
    # 启动Bot
    updater.start_polling()
    print(f"🤖 Telegram Bot 已启动 (@{BOT_USERNAME})")

    # 启动Flask Web服务
    app.template_folder = BASE_DIR
    print(f"🌐 Flask Web服务已启动 ({DOMAIN})")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)

    # 停止调度器
    scheduler.shutdown()
    updater.idle()