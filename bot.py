"""
Telegram 机器人 — 群聊/频道关键词监听 + 随机视频转发
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
功能 1：关键词转发链（支持群聊 + 频道）
  源群聊/频道消息 → 识别关键词 → 转发给指定机器人 → 机器人回复 → 转发到你的频道

功能 2：未知群聊自动发现
  机器人加入新群后，只要有人说话，日志就会打印群 ID 和名称

功能 3：随机视频
  /random 随机转发一条历史视频，/stats 查看统计
  使用 PostgreSQL 持久化存储（Zeabur 云数据库）
"""

import os
import time
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ==================== 环境变量 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("未设置 BOT_TOKEN")

# PostgreSQL 连接地址（Zeabur 云数据库提供）
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- 关键词转发链 ---
SOURCE_CHANNELS = [
    c.strip()
    for c in os.getenv("SOURCE_CHANNELS", "").split(",")
    if c.strip()
]
KEYWORDS = [
    k.strip()
    for k in os.getenv("KEYWORDS", "").split(",")
    if k.strip()
]
TARGET_BOT = os.getenv("TARGET_BOT", "").strip()
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "").strip()

# --- 随机视频 ---
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

# 防止重复打印发现日志
_discovered_chats: set[int] = set()

# PostgreSQL 连接
_pg_conn = None


# ==================== 工具函数 ====================
def is_chat_match(chat, config_id: str) -> bool:
    chat_id_str = str(chat.id)
    expected = config_id.lstrip("@")
    if chat_id_str == expected:
        return True
    if chat.username and f"@{chat.username}" == config_id:
        return True
    return False


def any_keyword_match(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def get_chat_label(chat) -> str:
    parts = [str(chat.id)]
    title = getattr(chat, "title", "") or ""
    username = getattr(chat, "username", "") or ""
    if title:
        parts.append(f"'{title}'")
    if username:
        parts.append(f"@{username}")
    return ", ".join(parts)


# ==================== 数据库（PostgreSQL）====================
def get_pg():
    """获取数据库连接，断线自动重连"""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        if not DATABASE_URL:
            return None
        try:
            _pg_conn = psycopg2.connect(DATABASE_URL)
            _pg_conn.autocommit = True
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 数据库连接失败: {e}")
            return None
    return _pg_conn


def init_db():
    if not DATABASE_URL:
        print("⚠️  DATABASE_URL 未设置，视频收录功能不可用")
        return

    conn = get_pg()
    if conn is None:
        return

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                chat_id TEXT NOT NULL,
                file_id TEXT,
                file_unique_id TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, message_id)
            )
        """)
    print("✅ PostgreSQL 数据库就绪")


def add_video(message_id: int, chat_id: str, file_id: str, file_unique_id: str):
    conn = get_pg()
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO videos (message_id, chat_id, file_id, file_unique_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (chat_id, message_id) DO NOTHING",
            (message_id, chat_id, file_id, file_unique_id),
        )


def get_random_video() -> dict | None:
    conn = get_pg()
    if conn is None:
        return None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT message_id, chat_id FROM videos ORDER BY RANDOM() LIMIT 1"
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_video_count() -> int:
    conn = get_pg()
    if conn is None:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM videos")
        row = cur.fetchone()
    return row[0] if row else 0


# ==================== 命令处理器 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是多功能转发机器人。\n\n"
        "📡 关键词转发链：监听群聊/频道 → 识别关键词 → 转给指定机器人 → 把回复转回你的频道\n\n"
        "🎲 命令列表：\n"
        "/random - 随机转发一条历史视频\n"
        "/stats  - 查看已收录的视频数量"
    )


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = get_random_video()
    if not video:
        await update.message.reply_text("📭 还没有收录任何视频，请先在频道中发布视频！")
        return

    try:
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=video["chat_id"],
            message_id=video["message_id"],
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 转发失败：{e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = get_video_count()
    await update.message.reply_text(f"📊 已收录 {count} 条视频")


# ==================== 核心：消息处理（群聊 + 频道）====================
async def on_source_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if msg is None:
        return

    chat = msg.chat
    chat_id = chat.id

    # ---------- 自动发现未知群聊 ----------
    source_list = SOURCE_CHANNELS
    is_source = any(is_chat_match(chat, ch) for ch in source_list)

    if not is_source and chat_id not in _discovered_chats:
        _discovered_chats.add(chat_id)
        chat_type = chat.type or "?"
        label = get_chat_label(chat)
        is_group = chat_type in ("group", "supergroup")
        print("=" * 50)
        print(f"[🔍 发现新{'群聊' if is_group else '频道'}]")
        print(f"    ID: {chat_id}")
        print(f"    类型: {chat_type}")
        print(f"    名称: {label}")
        print(f"    把上面这个 ID 加到 Zeabur 环境变量 SOURCE_CHANNELS 即可")
        print("=" * 50)

    # ---------- 关键词转发 ----------
    if not SOURCE_CHANNELS or not KEYWORDS or not TARGET_BOT or not DEST_CHANNEL:
        return

    if not is_source:
        return

    text = msg.text or msg.caption or ""
    if not any_keyword_match(text):
        return

    matched = [kw for kw in KEYWORDS if kw.lower() in text.lower()]
    try:
        await context.bot.forward_message(
            chat_id=TARGET_BOT,
            from_chat_id=chat_id,
            message_id=msg.message_id,
        )
        print(
            f"[{datetime.now()}] 🎯 命中 {matched} "
            f"→ 已转发至 {TARGET_BOT}（来源: {get_chat_label(chat)}）"
        )
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发至 {TARGET_BOT} 失败: {e}")


# ==================== 目标机器人回复 → 转发 ====================
async def on_target_bot_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return

    target_username = TARGET_BOT.lstrip("@")
    if not msg.from_user or msg.from_user.username != target_username:
        return

    try:
        await context.bot.forward_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        print(f"[{datetime.now()}] 📤 已转发 {TARGET_BOT} 的回复 → {DEST_CHANNEL}")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发至 {DEST_CHANNEL} 失败: {e}")


# ==================== 视频收录 ====================
async def on_channel_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None or msg.video is None:
        return

    if not CHANNEL_ID:
        return

    if not is_chat_match(msg.chat, CHANNEL_ID):
        return

    add_video(
        message_id=msg.message_id,
        chat_id=str(msg.chat_id),
        file_id=msg.video.file_id,
        file_unique_id=msg.video.file_unique_id,
    )
    print(f"[{datetime.now()}] ✅ 收录视频 message_id={msg.message_id}")


# ==================== 错误处理 ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    print(f"[{datetime.now()}] ⚠️ 运行时错误: {err}")


# ==================== 构建 Application ====================
def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_video))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_source_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_source_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, on_target_bot_reply))

    if CHANNEL_ID:
        app.add_handler(MessageHandler(
            filters.ChatType.CHANNEL & filters.VIDEO, on_channel_video,
        ))

    return app


# ==================== 主程序 ====================
def main():
    init_db()

    count = get_video_count()
    if DATABASE_URL:
        print(f"📦 数据库就绪，当前收录 {count} 条视频")

    print("=" * 50)
    print("当前配置：")
    print(f"  BOT_TOKEN:     {BOT_TOKEN[:8]}...（已隐藏）")
    db_info = "已配置" if DATABASE_URL else "未配置（视频功能不可用）"
    print(f"  数据库:        {db_info}")
    print(f"  源频道/群聊:   {SOURCE_CHANNELS or '(未配置，仅自动发现)'}")
    print(f"  关键词:        {KEYWORDS or '(未配置)'}")
    print(f"  目标机器人:    {TARGET_BOT or '(未配置)'}")
    print(f"  转发目的地:    {DEST_CHANNEL or '(未配置)'}")
    print(f"  视频收录频道:  {CHANNEL_ID or '(未配置)'}")
    print("=" * 50)

    print("✅ 群聊/频道监听已启用（自动发现 + 关键词转发）")
    print("✅ 目标机器人回复监听已启用")
    if CHANNEL_ID:
        print("✅ 视频收录已启用")

    print("🤖 机器人启动中...")
    retry_delay = 5

    while True:
        app = build_app()
        try:
            app.run_polling(
                allowed_updates=["channel_post", "message"],
                drop_pending_updates=True,
                poll_interval=1.0,
            )
        except (KeyboardInterrupt, SystemExit):
            print("🛑 收到退出信号，正在关闭...")
            sys.exit(0)
        except Exception as e:
            err_str = str(e)
            if "Conflict" in err_str:
                wait = 15
                print(f"⏳ 检测到旧实例未退出，等待 {wait} 秒...")
                time.sleep(wait)
            else:
                print(f"❌ 连接出错: {e}")
                print(f"   {retry_delay} 秒后重试...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
