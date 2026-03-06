import os
import json
import logging
import asyncio
import random
import sqlite3
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# استيراد خادم الويب الوهمي (لابد من وجود ملف server.py)
from server import keep_alive

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

# ======================== حالة الألعاب النشطة ========================

active_games: Dict[int, Dict] = {}

# ========================= دوال مساعدة =========================

def format_question_text(question: Dict, q_num: int, total: int) -> str:
    """تنسيق نص السؤال فقط (بدون خيارات)"""
    return (
        f"📝 **السؤال {q_num}/{total}**\n"
        f"{question['text']}\n"
        f"⏳ لديك 20 ثانية للإجابة"
    )

def build_options_keyboard(question: Dict, game_id: str) -> InlineKeyboardMarkup:
    """بناء أزرار الخيارات مع بيانات callback تحتوي على معرف اللعبة ورقم الخيار"""
    keyboard = []
    for i, option in enumerate(question["options"]):
        callback_data = f"{game_id}:{i}"
        keyboard.append([InlineKeyboardButton(option, callback_data=callback_data)])
    return InlineKeyboardMarkup(keyboard)

async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إرسال السؤال الحالي للمجموعة مع أزرار الخيارات"""
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
    text = format_question_text(question, q_index + 1, total)

    # إنشاء معرف فريد لهذه اللعبة (chat_id + index) لاستخدامه في callback
    game_id = f"{chat_id}_{q_index}"

    # بناء الأزرار
    reply_markup = build_options_keyboard(question, game_id)

    # إرسال السؤال مع الأزرار
    msg = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")

    # حفظ معلومات الجولة الحالية
    game["current_q_msg_id"] = msg.message_id
    game["q_start_time"] = asyncio.get_event_loop().time()
    game["answered_users"] = set()
    game["correct_answer_given"] = False
    game["current_game_id"] = game_id

    # جدولة إنهاء السؤال بعد 20 ثانية
    asyncio.create_task(handle_question_timeout(chat_id, context, 20, msg.message_id))

async def handle_question_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, delay: int, msg_id: int):
    """معالجة انتهاء وقت السؤال"""
    await asyncio.sleep(delay)
    game = active_games.get(chat_id)
    if not game or game["status"] != "playing" or game["current_q_msg_id"] != msg_id:
        return

    if not game.get("correct_answer_given"):
        question = game["questions"][game["current_q_index"]]
        correct_option = question["options"][question["correct_index"]]
        # إرسال رسالة بالإجابة الصحيحة
        await context.bot.send_message(
            chat_id,
            f"⏰ انتهى الوقت!\nالإجابة الصحيحة: **{correct_option}**",
            parse_mode="Markdown"
        )
        # تعطيل الأزرار
        try:
            await context.bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except:
            pass

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

# ======================== إعداد قائمة الأوامر (Commands Menu) ========================

async def set_commands(application: Application):
    """تعيين قائمة الأوامر التي تظهر عند كتابة /"""
    commands = [
        ("start", "بدء البوت والترحيب"),
        ("play", "بدء لعبة جديدة في المجموعة"),
        ("menu", "عرض أزرار الأوامر"),
        ("score", "عرض نقاطك الإجمالية"),
        ("score_group", "عرض نقاطك في هذه المجموعة"),
        ("leaderboard", "ترتيب اللاعبين في المجموعة"),
        ("help", "عرض المساعدة"),
        ("endgame", "إنهاء اللعبة الحالية (للمشرفين)"),
    ]
    await application.bot.set_my_commands(commands)

# ======================== معالجات الأوامر ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب مع عرض الأزرار"""
    keyboard = [
        [KeyboardButton("/play"), KeyboardButton("/menu")],
        [KeyboardButton("/score"), KeyboardButton("/leaderboard")],
        [KeyboardButton("/help")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    
    await update.message.reply_text(
        "مرحباً! أنا بوت أسئلة وألغاز جماعي.\n"
        "استخدم الأزرار أدناه أو اكتب الأوامر مباشرة.\n"
        "لبدء لعبة في المجموعة، أرسل /play\n"
        "لعرض الأزرار مرة أخرى، أرسل /menu",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **أوامر البوت:**
/play - بدء لعبة جديدة في المجموعة
/endgame - إنهاء اللعبة الحالية (للمشرفين)
/score - عرض نقاطك الإجمالية
/score_group - عرض نقاطك في هذه المجموعة
/leaderboard - ترتيب أفضل اللاعبين في هذه المجموعة
/menu - عرض أزرار الأوامر
/help - عرض هذه المساعدة

🎮 **كيفية اللعب:**
- أرسل /play واختر عدد الأسئلة.
- سيتم طرح الأسئلة واحداً تلو الآخر مع أزرار للإجابة.
- أسرع من يضغط على الزر الصحيح يحصل على نقطة.
- في نهاية اللعبة يظهر الفائز.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الأزرار التفاعلية"""
    keyboard = [
        [KeyboardButton("/play"), KeyboardButton("/score")],
        [KeyboardButton("/leaderboard"), KeyboardButton("/score_group")],
        [KeyboardButton("/help"), KeyboardButton("/menu")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("📋 اختر أمراً من الأزرار أدناه:", reply_markup=reply_markup)

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        await update.message.reply_text("⚠️ توجد لعبة نشطة حالياً. انهها أولاً بـ /endgame")
        return

    if not QUESTIONS:
        await update.message.reply_text("❌ عذراً، لا توجد أسئلة متاحة حالياً. راجع المشرف.")
        return

    keyboard = [[
        InlineKeyboardButton("5", callback_data="play_5"),
        InlineKeyboardButton("10", callback_data="play_10"),
        InlineKeyboardButton("15", callback_data="play_15"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎮 اختر عدد الأسئلة:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على الأزرار (اختيار عدد الأسئلة أو إجابة)"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    data = query.data

    # إذا كان الضغط على زر اختيار عدد الأسئلة
    if data.startswith("play_"):
        num_questions = int(data.split("_")[1])
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
        return

    # معالجة إجابة على سؤال (البيانات تأتي بصيغة game_id:option_index)
    try:
        game_id, option_index = data.split(":")
        option_index = int(option_index)
        last_underscore = game_id.rfind("_")
        if last_underscore == -1:
            return
        original_chat_id = int(game_id[:last_underscore])
        q_index = int(game_id[last_underscore+1:])
    except Exception as e:
        logger.error(f"خطأ في تحليل callback data: {e}")
        return

    game = active_games.get(original_chat_id)
    if not game or game["status"] != "playing":
        await query.edit_message_text("❌ اللعبة انتهت أو لم تعد موجودة.")
        return

    if game["current_q_index"] != q_index:
        await query.message.reply_text("⚠️ هذا السؤال قد انتهى بالفعل.")
        return

    if user_id in game.get("answered_users", set()):
        await query.message.reply_text("⚠️ لقد أجبت بالفعل على هذا السؤال.")
        return

    question = game["questions"][q_index]
    correct = (option_index == question["correct_index"])

    if correct:
        game["answered_users"].add(user_id)
        if not game.get("correct_answer_given"):
            game["correct_answer_given"] = True
            game["players"][user_id] = game["players"].get(user_id, 0) + 1

            first_name = query.from_user.first_name
            await context.bot.send_message(
                original_chat_id,
                f"✅ {first_name} كان الأسرع! (+1 نقطة)"
            )

            # تعطيل جميع الأزرار بعد الإجابة الصحيحة الأولى
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except:
                pass
        else:
            await query.message.reply_text("✅ إجابة صحيحة، ولكن هناك من سبقك!")
    else:
        await query.message.reply_text("❌ إجابة خاطئة!")

async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in active_games:
        await update.message.reply_text("❌ لا توجد لعبة نشطة حالياً.")
        return

    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمشرفين.")
        return

    await end_game(chat_id, context)
    await update.message.reply_text("✅ تم إنهاء اللعبة.")

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    points = get_user_total_points(user_id)
    await update.message.reply_text(f"🏅 مجموع نقاطك الإجمالية: {points}")

async def score_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    points = get_group_points(chat_id, user_id)
    await update.message.reply_text(f"📊 نقاطك في هذه المجموعة: {points}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# معالج الرسائل النصية العادية (لتحويل النقر على أزرار الـ Reply Keyboard إلى أوامر)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # إذا كان النص يطابق أمراً من الأزرار، نوجهه إلى المعالج المناسب
    if text == "/play":
        await play(update, context)
    elif text == "/menu":
        await menu_command(update, context)
    elif text == "/score":
        await score(update, context)
    elif text == "/score_group":
        await score_group(update, context)
    elif text == "/leaderboard":
        await leaderboard(update, context)
    elif text == "/help":
        await help_command(update, context)
    elif text == "/endgame":
        await endgame(update, context)
    # أي نص آخر يمكن تجاهله

# ======================== تشغيل البوت ========================

def main():
    if not TOKEN:
        logger.error("❌ BOT_TOKEN غير موجود في متغيرات البيئة.")
        return

    global QUESTIONS
    QUESTIONS = load_questions_from_json()
    if not QUESTIONS:
        logger.error("❌ لم يتم تحميل أي أسئلة. تأكد من وجود questions.json.")
        # يمكن المتابعة ولكن البوت لن يعمل بشكل صحيح

    try:
        init_db()
        logger.info("✅ تم تهيئة قاعدة البيانات SQLite.")
    except Exception as e:
        logger.exception("❌ فشل في تهيئة قاعدة البيانات: %s", e)
        return

    application = Application.builder().token(TOKEN).build()

    # تعيين قائمة الأوامر
    asyncio.get_event_loop().run_until_complete(set_commands(application))

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("endgame", endgame))
    application.add_handler(CommandHandler("score", score))
    application.add_handler(CommandHandler("score_group", score_group))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CallbackQueryHandler(button_callback))
    # معالج الرسائل النصية (للأزرار)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # تشغيل خادم الويب الوهمي (لأجل Web Service على Render)
    keep_alive()
    logger.info(f"✅ خادم الويب الوهمي يعمل على المنفذ {os.getenv('PORT', '8080')}")

    logger.info("✅ البوت يعمل...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("حدث خطأ غير متوقع: %s", e)
        import sys
        print(f"FATAL ERROR: {e}", file=sys.stderr)
        raise
