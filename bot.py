"""
Telegram 机器人 - 随机转发频道视频
功能：
  - 自动记录频道中发布的视频
  - /random 命令：随机转发一条已记录的视频给用户
  - /stats 命令：查看已记录的视频数量
"""

import os
import sqlite3
from datetime import datetime

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

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
# 你的频道 ID，支持两种格式：
#   @username  例如 "@my_channel"
#   -100xxx    例如 "-1001234567890"
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    raise RuntimeError("未设置 BOT_TOKEN，请在 .env 文件中配置")
if not CHANNEL_ID:
    raise RuntimeError("未设置 CHANNEL_ID，请在 .env 文件中配置")

DB_PATH = "videos.db"


# ==================== 数据库 ====================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                file_id TEXT,
                file_unique_id TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 防止重复记录同一个视频
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_video_unique
            ON videos(chat_id, message_id)
        """)


def add_video(message_id: int, chat_id: str, file_id: str, file_unique_id: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO videos (message_id, chat_id, file_id, file_unique_id) "
            "VALUES (?, ?, ?, ?)",
            (message_id, chat_id, file_id, file_unique_id),
        )


def get_random_video() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT message_id, chat_id FROM videos ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_video_count() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM videos").fetchone()
    return row["cnt"] if row else 0


# ==================== 机器人逻辑 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 命令"""
    await update.message.reply_text(
        "👋 你好！我是一个随机视频机器人。\n\n"
        "我会自动记录频道中的视频，发送 /random 即可随机获取一条视频！\n\n"
        "命令列表：\n"
        "/random - 随机转发一条视频\n"
        "/stats  - 查看已记录的视频数量"
    )


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/random - 随机转发一条视频"""
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
    """/stats - 查看已收录的视频数量"""
    count = get_video_count()
    await update.message.reply_text(f"📊 已收录 {count} 条视频")


async def on_channel_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """当频道发布新视频时，自动记录到数据库"""
    msg = update.channel_post
    if msg is None or msg.video is None:
        return

    # 只记录指定频道的视频
    chat_id_str = str(msg.chat_id)
    expected_chat_id = CHANNEL_ID.lstrip("@")

    # 匹配频道 ID（支持 @username 和数字 ID 两种格式）
    is_target = (
        chat_id_str == expected_chat_id
        or f"@{msg.chat.username}" == CHANNEL_ID
        if msg.chat.username
        else chat_id_str == expected_chat_id
    )

    if not is_target:
        return

    add_video(
        message_id=msg.message_id,
        chat_id=chat_id_str,
        file_id=msg.video.file_id,
        file_unique_id=msg.video.file_unique_id,
    )
    print(f"[{datetime.now()}] ✅ 收录视频 message_id={msg.message_id}")


# ==================== 主程序 ====================
def main():
    init_db()
    print(f"📦 数据库已就绪，当前收录 {get_video_count()} 条视频")

    app = Application.builder().token(BOT_TOKEN).build()

    # 注册命令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_video))
    app.add_handler(CommandHandler("stats", stats))

    # 监听频道视频发布（机器人需是频道管理员）
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.VIDEO,
            on_channel_video,
        )
    )

    print("🤖 机器人启动中...")
    app.run_polling(
        allowed_updates=["channel_post", "message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
