import os
import json
import logging
import asyncio
import random
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# تحميل متغيرات البيئة
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# إعداد السجلات
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================== قاعدة بيانات SQLite ========================

DB_PATH = "quiz_bot.db"

def init_db():
    """إنشاء الجداول إذا لم تكن موجودة"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # جدول المستخدمين (النقاط الإجمالية)
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            total_points INTEGER DEFAULT 0,
            first_name TEXT,
            username TEXT
        )
    ''')
    # جدول نقاط المجموعات
    c.execute('''
        CREATE TABLE IF NOT EXISTS group_scores (
            chat_id INTEGER,
            user_id INTEGER,
            points INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    conn.commit()
    conn.close()

def get_user_total_points(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_points FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def update_user_total_points(user_id: int, points_to_add: int, first_name: str = None, username: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # تحديث أو إدراج المستخدم
    c.execute('''
        INSERT INTO users (user_id, total_points, first_name, username)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            total_points = total_points + excluded.total_points,
            first_name = COALESCE(excluded.first_name, first_name),
            username = COALESCE(excluded.username, username)
    ''', (user_id, points_to_add, first_name, username))
    conn.commit()
    conn.close()

def get_group_points(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT points FROM group_scores WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def update_group_points(chat_id: int, user_id: int, points_to_add: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO group_scores (chat_id, user_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            points = points + excluded.points
    ''', (chat_id, user_id, points_to_add))
    conn.commit()
    conn.close()

def get_group_leaderboard(chat_id: int, limit: int = 10) -> List[tuple]:
    """إرجاع قائمة بأفضل اللاعبين في مجموعة (user_id, points)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT user_id, points FROM group_scores
        WHERE chat_id = ?
        ORDER BY points DESC
        LIMIT ?
    ''', (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

# ======================== تحميل الأسئلة من ملف JSON ========================

def load_questions_from_json(file_path: str = "questions.json") -> List[Dict]:
    """تحميل قائمة الأسئلة من ملف JSON"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            questions = json.load(f)
        logger.info(f"تم تحميل {len(questions)} سؤال من {file_path}")
        return questions
    except FileNotFoundError:
        logger.error(f"ملف {file_path} غير موجود. تأكد من وجوده بجانب main.py")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"خطأ في قراءة ملف JSON: {e}")
        return []

QUESTIONS = load_questions_from_json()

if not QUESTIONS:
    logger.warning("لا توجد أسئلة! البوت لن يعمل بشكل صحيح.")

def get_random_questions(count: int = 5) -> List[Dict]:
    """إرجاع قائمة عشوائية من الأسئلة"""
    if not QUESTIONS:
        return []
    return random.sample(QUESTIONS, min(count, len(QUESTIONS)))

# ======================== حالة الألعاب النشطة (في الذاكرة) ========================

active_games: Dict[int, Dict] = {}

# ========================= دوال مساعدة =========================

def format_question(question: Dict, q_num: int, total: int) -> str:
    """تنسيق نص السؤال"""
    if question["type"] == "choice":
        options_text = "\n".join(
            [f"{i+1}. {opt}" for i, opt in enumerate(question["options"])]
        )
        return (
            f"📝 **السؤال {q_num}/{total}**\n"
            f"{question['text']}\n\n"
            f"{options_text}\n\n"
            f"⏳ لديك 20 ثانية للإجابة (أرسل رقم الإجابة)"
        )
    else:  # riddle
        return (
            f"🧩 **لغز {q_num}/{total}**\n"
            f"{question['text']}\n\n"
            f"⏳ لديك 30 ثانية للإجابة (أرسل الإجابة نصياً)"
        )

async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إرسال السؤال الحالي للمجموعة"""
    game = active_games.get(chat_id)
    if not game or game["status"] != "playing":
        return

    q_index = game["current_q_index"]
    questions = game["questions"]
    if q_index >= len(questions):
        await end_game(chat_id, context)
        return

    question = questions[q_index]
    total = len(questions)
    text = format_question(question, q_index + 1, total)

    # إرسال السؤال
    msg = await context.bot.send_message(chat_id, text)

    # حفظ معلومات الجولة الحالية
    game["current_q_msg_id"] = msg.message_id
    game["q_start_time"] = asyncio.get_event_loop().time()
    game["answered_users"] = set()
    game["correct_answer_given"] = False

    # تحديد المهلة
    timeout = 20 if question["type"] == "choice" else 30
    asyncio.create_task(handle_question_timeout(chat_id, context, timeout))

async def handle_question_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, delay: int):
    """معالجة انتهاء وقت السؤال"""
    await asyncio.sleep(delay)
    game = active_games.get(chat_id)
    if not game or game["status"] != "playing":
        return

    if not game.get("correct_answer_given"):
        question = game["questions"][game["current_q_index"]]
        if question["type"] == "choice":
            correct_option = question["options"][question["correct_index"]]
            await context.bot.send_message(
                chat_id,
                f"⏰ انتهى الوقت!\nالإجابة الصحيحة: {correct_option}"
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"⏰ انتهى الوقت!\nالإجابة الصحيحة: {question['answer']}"
            )

    # الانتقال للسؤال التالي
    game["current_q_index"] += 1
    await asyncio.sleep(2)
    await send_question(chat_id, context)

async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إنهاء اللعبة وإعلان النتائج وتحديث قاعدة البيانات"""
    game = active_games.pop(chat_id, None)
    if not game:
        return

    players = game.get("players", {})
    if players:
        # ترتيب اللاعبين
        sorted_players = sorted(players.items(), key=lambda x: x[1], reverse=True)
        result_text = "🏆 **انتهت اللعبة!**\n\nالنتائج النهائية:\n"
        for idx, (user_id, points) in enumerate(sorted_players, 1):
            # الحصول على معلومات المستخدم
            try:
                user = await context.bot.get_chat(user_id)
                first_name = user.first_name or f"مستخدم {user_id}"
                username = user.username
            except:
                first_name = f"مستخدم {user_id}"
                username = None

            result_text += f"{idx}. {first_name}: {points} نقطة\n"

            # تحديث النقاط في قاعدة البيانات
            update_user_total_points(user_id, points, first_name, username)
            update_group_points(chat_id, user_id, points)

        # إضافة لوحة الشرف للمجموعة
        result_text += "\n📊 **ترتيب المجموعة العام**\n"
        top_group = get_group_leaderboard(chat_id, 5)
        for i, (uid, pts) in enumerate(top_group, 1):
            try:
                user = await context.bot.get_chat(uid)
                name = user.first_name or f"مستخدم {uid}"
            except:
                name = f"مستخدم {uid}"
            result_text += f"{i}. {name}: {pts} نقطة\n"
    else:
        result_text = "🏁 انتهت اللعبة دون مشاركة."

    await context.bot.send_message(chat_id, result_text, parse_mode="Markdown")

# ======================== معالجات الأوامر ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً! أنا بوت أسئلة وألغاز جماعي.\n"
        "لبدء لعبة في المجموعة، أرسل /play\n"
        "للأوامر المتاحة: /help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **أوامر البوت:**
/play - بدء لعبة جديدة في المجموعة
/endgame - إنهاء اللعبة الحالية (للمشرفين)
/score - عرض نقاطك الإجمالية
/score_group - عرض نقاطك في هذه المجموعة
/leaderboard - ترتيب أفضل اللاعبين في هذه المجموعة
/help - عرض هذه المساعدة

🎮 **كيفية اللعب:**
- أرسل /play واختر عدد الأسئلة.
- سيتم طرح الأسئلة واحداً تلو الآخر.
- أسرع إجابة صحيحة تحصل على نقطة.
- في نهاية اللعبة يظهر الفائز.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        await update.message.reply_text("⚠️ توجد لعبة نشطة حالياً. انهها أولاً بـ /endgame")
        return

    # التحقق من وجود أسئلة
    if not QUESTIONS:
        await update.message.reply_text("❌ عذراً، لا توجد أسئلة متاحة حالياً. راجع المشرف.")
        return

    keyboard = [[
        InlineKeyboardButton("5", callback_data="5"),
        InlineKeyboardButton("10", callback_data="10"),
        InlineKeyboardButton("15", callback_data="15"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎮 اختر عدد الأسئلة:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if query.data.isdigit():
        num_questions = int(query.data)
        active_games[chat_id] = {
            "status": "playing",
            "players": {},
            "questions": get_random_questions(num_questions),
            "current_q_index": 0,
            "started_by": user_id,
        }
        await query.edit_message_text(
            f"🚀 تم بدء لعبة جديدة بـ {num_questions} أسئلة!\n"
            "سيبدأ السؤال الأول بعد 5 ثوانٍ... جهزوا أنفسكم 😉"
        )
        await asyncio.sleep(5)
        await send_question(chat_id, context)

async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in active_games:
        await update.message.reply_text("❌ لا توجد لعبة نشطة حالياً.")
        return

    # التحقق من صلاحية المشرف
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمشرفين.")
        return

    await end_game(chat_id, context)
    await update.message.reply_text("✅ تم إنهاء اللعبة.")

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض النقاط الإجمالية للمستخدم"""
    user_id = update.effective_user.id
    points = get_user_total_points(user_id)
    await update.message.reply_text(f"🏅 مجموع نقاطك الإجمالية: {points}")

async def score_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض نقاط المستخدم في هذه المجموعة"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    points = get_group_points(chat_id, user_id)
    await update.message.reply_text(f"📊 نقاطك في هذه المجموعة: {points}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض ترتيب المجموعة"""
    chat_id = update.effective_chat.id
    top_users = get_group_leaderboard(chat_id, 10)

    if not top_users:
        await update.message.reply_text("📊 لا توجد نقاط مسجلة في هذه المجموعة بعد.")
        return

    text = "🏆 **ترتيب اللاعبين في هذه المجموعة:**\n\n"
    for idx, (uid, pts) in enumerate(top_users, 1):
        try:
            user = await context.bot.get_chat(uid)
            name = user.first_name or f"مستخدم {uid}"
        except:
            name = f"مستخدم {uid}"
        text += f"{idx}. {name}: {pts} نقطة\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الإجابات"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text.strip()

    game = active_games.get(chat_id)
    if not game or game["status"] != "playing":
        return

    if "current_q_index" not in game or game["current_q_index"] >= len(game["questions"]):
        return

    question = game["questions"][game["current_q_index"]]

    if user_id in game.get("answered_users", set()):
        return  # سبق أن أجاب

    correct = False
    if question["type"] == "choice":
        if text.isdigit():
            choice_index = int(text) - 1
            if choice_index == question["correct_index"]:
                correct = True
    else:  # riddle
        if text.strip().lower() == question["answer"].lower():
            correct = True

    if correct:
        game["answered_users"].add(user_id)
        if not game.get("correct_answer_given"):
            game["correct_answer_given"] = True
            game["players"][user_id] = game["players"].get(user_id, 0) + 1

            first_name = update.effective_user.first_name
            await context.bot.send_message(
                chat_id,
                f"✅ {first_name} كان الأسرع! (+1 نقطة)"
            )
        else:
            await update.message.reply_text("✅ إجابة صحيحة، ولكن هناك من سبقك!")
    # else: يمكن تجاهل الإجابة الخاطئة أو الرد برسالة خاصة (اختياري)

# ======================== تشغيل البوت ========================

def main():
    if not TOKEN:
        logger.error("لم يتم تعيين BOT_TOKEN في متغيرات البيئة.")
        return

    # تهيئة قاعدة البيانات
    init_db()
    logger.info("تم تهيئة قاعدة البيانات SQLite.")

    application = Application.builder().token(TOKEN).build()

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("endgame", endgame))
    application.add_handler(CommandHandler("score", score))
    application.add_handler(CommandHandler("score_group", score_group))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("البوت يعمل...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
