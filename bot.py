"""
Telegram 机器人 — 多渠道关键词监听 + 随机视频转发
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
功能 1：关键词转发链
  源频道发布消息 → 识别关键词 → 转发给指定机器人 → 机器人回复 → 转发回复到你的频道

功能 2：随机视频
  自动记录频道中的视频，/random 随机转发，/stats 查看统计
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

# ==================== 环境变量 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("未设置 BOT_TOKEN")

# --- 关键词转发链 ---
# 监听的源频道，逗号分隔。例如: @chan1,@chan2,-100xxx
SOURCE_CHANNELS = [
    c.strip()
    for c in os.getenv("SOURCE_CHANNELS", "").split(",")
    if c.strip()
]
# 触发关键词，逗号分隔。命中任意一个即触发
KEYWORDS = [
    k.strip()
    for k in os.getenv("KEYWORDS", "").split(",")
    if k.strip()
]
# 目标机器人（接收转发消息的机器人）
TARGET_BOT = os.getenv("TARGET_BOT", "").strip()
# 回复转发目的地（你的频道）
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "").strip()

# --- 随机视频（保留旧功能）---
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

DB_PATH = "videos.db"


# ==================== 工具函数 ====================
def is_channel_match(chat, config_id: str) -> bool:
    """判断一个 chat 是否匹配配置中的频道 ID（支持 @username 和数字 ID）"""
    chat_id_str = str(chat.id)
    expected = config_id.lstrip("@")

    if chat_id_str == expected:
        return True
    if chat.username and f"@{chat.username}" == config_id:
        return True
    return False


def any_keyword_match(text: str) -> bool:
    """文本中是否包含任意一个关键词（不区分大小写）"""
    if not text:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


# ==================== 数据库（随机视频功能）====================
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


# ==================== 命令处理器 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是多功能转发机器人。\n\n"
        "📡 关键词转发链：监听多个频道 → 识别关键词 → 转给指定机器人 → 把回复转回你的频道\n\n"
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


# ==================== 核心：关键词转发链 ====================
async def on_source_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """源频道有新消息 → 检查关键词 → 转发给目标机器人"""
    msg = update.channel_post
    if msg is None:
        return

    # 检查这条消息是否来自我们监听的源频道
    is_source = any(is_channel_match(msg.chat, ch) for ch in SOURCE_CHANNELS)
    if not is_source:
        return

    # 提取消息文本（支持 caption）
    text = msg.text or msg.caption or ""

    # 检查关键词
    if not any_keyword_match(text):
        return

    # 命中关键词！转发给目标机器人
    try:
        await context.bot.forward_message(
            chat_id=TARGET_BOT,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        print(
            f"[{datetime.now()}] 🎯 命中关键词 → 已转发给 {TARGET_BOT} "
            f"（来源: {msg.chat_id}, 关键词匹配: "
            f"{[kw for kw in KEYWORDS if kw.lower() in text.lower()]}）"
        )
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发给 {TARGET_BOT} 失败：{e}")


async def on_target_bot_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """目标机器人回复了 → 转发回复到目标频道"""
    msg = update.message
    if msg is None:
        return

    # 只处理来自目标机器人的私聊消息
    target_username = TARGET_BOT.lstrip("@")
    if not msg.from_user or msg.from_user.username != target_username:
        return

    # 转发机器人的回复到目标频道
    try:
        await context.bot.forward_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        print(
            f"[{datetime.now()}] 📤 已转发 {TARGET_BOT} 的回复到 {DEST_CHANNEL}"
        )
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发到 {DEST_CHANNEL} 失败：{e}")


# ==================== 随机视频收录 ====================
async def on_channel_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """频道发布视频 → 记录到数据库（供 /random 使用）"""
    msg = update.channel_post
    if msg is None or msg.video is None:
        return

    if not CHANNEL_ID:
        return

    if not is_channel_match(msg.chat, CHANNEL_ID):
        return

    add_video(
        message_id=msg.message_id,
        chat_id=str(msg.chat_id),
        file_id=msg.video.file_id,
        file_unique_id=msg.video.file_unique_id,
    )
    print(f"[{datetime.now()}] ✅ 收录视频 message_id={msg.message_id}")


# ==================== 主程序 ====================
def main():
    init_db()
    print(f"📦 数据库就绪，当前收录 {get_video_count()} 条视频")

    # 打印当前配置
    if SOURCE_CHANNELS:
        print(f"📡 监听频道: {SOURCE_CHANNELS}")
        print(f"🔑 关键词: {KEYWORDS}")
        print(f"🤖 目标机器人: {TARGET_BOT}")
        print(f"📢 转发目的地: {DEST_CHANNEL}")
    else:
        print("⚠️  未配置 SOURCE_CHANNELS，关键词转发功能未启用")
    if CHANNEL_ID:
        print(f"🎥 视频收录频道: {CHANNEL_ID}")

    app = Application.builder().token(BOT_TOKEN).build()

    # --- 命令 ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_video))
    app.add_handler(CommandHandler("stats", stats))

    # --- 关键词转发链 ---
    if SOURCE_CHANNELS and KEYWORDS and TARGET_BOT and DEST_CHANNEL:
        # 监听源频道的所有消息（不只是视频）
        app.add_handler(
            MessageHandler(
                filters.ChatType.CHANNEL,
                on_source_channel_post,
            )
        )
        # 监听目标机器人在私聊中的回复
        app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE,
                on_target_bot_reply,
            )
        )
        print("✅ 关键词转发链已启用")
    else:
        print("⚠️  关键词转发链未完整配置，已禁用")

    # --- 视频收录 ---
    if CHANNEL_ID:
        app.add_handler(
            MessageHandler(
                filters.ChatType.CHANNEL & filters.VIDEO,
                on_channel_video,
            )
        )
        print("✅ 视频收录已启用")

    print("🤖 机器人启动中...")
    app.run_polling(
        allowed_updates=["channel_post", "message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
