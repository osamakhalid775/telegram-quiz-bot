import os
import logging
import asyncio
import random
from datetime import datetime
from typing import Dict, Set, List, Optional

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

# إعداد السجلات (logs)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================== قاعدة بيانات بسيطة (في الذاكرة) ========================
# يمكن استبدالها بقاعدة بيانات حقيقية (SQLite/PostgreSQL) للتوسع

# تخزين الأسئلة (يمكن تحميلها من ملف أو قاعدة بيانات)
QUESTIONS = [
    {
        "id": 1,
        "type": "choice",
        "text": "ما عاصمة فرنسا؟",
        "options": ["لندن", "باريس", "روما", "مدريد"],
        "correct_index": 1,  # 0-based
        "category": "جغرافيا",
        "difficulty": "سهل",
    },
    {
        "id": 2,
        "type": "choice",
        "text": "كم عدد الكواكب في المجموعة الشمسية؟",
        "options": ["7", "8", "9", "10"],
        "correct_index": 1,
        "category": "علوم",
        "difficulty": "سهل",
    },
    {
        "id": 3,
        "type": "choice",
        "text": "من هو مؤلف رواية 'البؤساء'؟",
        "options": ["تشارلز ديكنز", "فيكتور هوجو", "ليو تولستوي", "مارك توين"],
        "correct_index": 1,
        "category": "أدب",
        "difficulty": "متوسط",
    },
    {
        "id": 4,
        "type": "riddle",
        "text": "شيء كلما أخذت منه يكبر؟",
        "answer": "الحفرة",
        "hint": "تحفر في الأرض",
        "difficulty": "متوسط",
    },
    {
        "id": 5,
        "type": "riddle",
        "text": "ما الشيء الذي يمشي بلا أرجل ويبكي بلا عيون؟",
        "answer": "السحاب",
        "hint": "في السماء",
        "difficulty": "صعب",
    },
]

# تخزين الألعاب النشطة لكل مجموعة (chat_id -> game_data)
active_games: Dict[int, Dict] = {}

# تخزين نقاط اللاعبين (user_id -> points) - يمكن حفظها بقاعدة بيانات
player_points: Dict[int, int] = {}

# تخزين نتائج المجموعات (chat_id -> {user_id: points})
group_leaderboards: Dict[int, Dict[int, int]] = {}

# ========================= دوال مساعدة =========================

def get_random_questions(count: int = 5) -> List[Dict]:
    """إرجاع قائمة عشوائية من الأسئلة"""
    return random.sample(QUESTIONS, min(count, len(QUESTIONS)))

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
    game["answered_users"] = set()  # من أجابوا على هذا السؤال
    game["correct_answer_given"] = False  # هل تم إعطاء إجابة صحيحة بالفعل؟

    # تحديد المهلة حسب نوع السؤال
    timeout = 20 if question["type"] == "choice" else 30
    # جدولة إنهاء السؤال بعد المهلة
    asyncio.create_task(handle_question_timeout(chat_id, context, timeout))

async def handle_question_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, delay: int):
    """معالجة انتهاء وقت السؤال"""
    await asyncio.sleep(delay)
    game = active_games.get(chat_id)
    if not game or game["status"] != "playing":
        return

    # إذا لم يتم إعطاء إجابة صحيحة بعد
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
    # تأخير بسيط قبل السؤال التالي
    await asyncio.sleep(2)
    await send_question(chat_id, context)

async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """إنهاء اللعبة وإعلان النتائج"""
    game = active_games.pop(chat_id, None)
    if not game:
        return

    players = game.get("players", {})
    if players:
        # ترتيب اللاعبين تنازلياً حسب النقاط
        sorted_players = sorted(players.items(), key=lambda x: x[1], reverse=True)
        result_text = "🏆 **انتهت اللعبة!**\n\nالنتائج النهائية:\n"
        for idx, (user_id, points) in enumerate(sorted_players, 1):
            # محاولة الحصول على اسم المستخدم
            try:
                user = await context.bot.get_chat(user_id)
                name = user.first_name or f"مستخدم {user_id}"
            except:
                name = f"مستخدم {user_id}"
            result_text += f"{idx}. {name}: {points} نقطة\n"

            # تحديث النقاط العامة والترتيب
            player_points[user_id] = player_points.get(user_id, 0) + points
            # تحديث ترتيب المجموعة
            if chat_id not in group_leaderboards:
                group_leaderboards[chat_id] = {}
            group_leaderboards[chat_id][user_id] = group_leaderboards[chat_id].get(user_id, 0) + points
    else:
        result_text = "🏁 انتهت اللعبة دون مشاركة."

    await context.bot.send_message(chat_id, result_text)

# ======================== معالجات الأوامر ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب"""
    await update.message.reply_text(
        "مرحباً! أنا بوت أسئلة وألغاز جماعي.\n"
        "لبدء لعبة في المجموعة، أرسل /play\n"
        "للأوامر المتاحة: /help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض المساعدة"""
    help_text = """
🤖 **أوامر البوت:**
/play - بدء لعبة جديدة في المجموعة
/endgame - إنهاء اللعبة الحالية (للمشرفين)
/score - عرض نقاطك
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
    """بدء لعبة جديدة"""
    chat_id = update.effective_chat.id

    # التحقق من وجود لعبة نشطة
    if chat_id in active_games:
        await update.message.reply_text("⚠️ توجد لعبة نشطة حالياً. انهها أولاً بـ /endgame")
        return

    # خيارات عدد الأسئلة
    keyboard = [
        [
            InlineKeyboardButton("5", callback_data="5"),
            InlineKeyboardButton("10", callback_data="10"),
            InlineKeyboardButton("15", callback_data="15"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎮 اختر عدد الأسئلة:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على الأزرار"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    # إذا كان الزر هو اختيار عدد الأسئلة
    if query.data.isdigit():
        num_questions = int(query.data)

        # إنشاء لعبة جديدة
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
    """إنهاء اللعبة (للمشرفين فقط)"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # التحقق من وجود لعبة
    if chat_id not in active_games:
        await update.message.reply_text("❌ لا توجد لعبة نشطة حالياً.")
        return

    # التحقق من صلاحية المشرف
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ["administrator", "creator"]:
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمشرفين.")
        return

    # إنهاء اللعبة
    await end_game(chat_id, context)
    await update.message.reply_text("✅ تم إنهاء اللعبة.")

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض نقاط المستخدم"""
    user_id = update.effective_user.id
    points = player_points.get(user_id, 0)
    await update.message.reply_text(f"🏅 مجموع نقاطك: {points}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض ترتيب المجموعة"""
    chat_id = update.effective_chat.id
    group_scores = group_leaderboards.get(chat_id, {})

    if not group_scores:
        await update.message.reply_text("📊 لا توجد نقاط مسجلة في هذه المجموعة بعد.")
        return

    # ترتيب تنازلي
    sorted_scores = sorted(group_scores.items(), key=lambda x: x[1], reverse=True)[:10]  # أفضل 10

    text = "🏆 **ترتيب اللاعبين في هذه المجموعة:**\n\n"
    for idx, (uid, pts) in enumerate(sorted_scores, 1):
        try:
            user = await context.bot.get_chat(uid)
            name = user.first_name or f"مستخدم {uid}"
        except:
            name = f"مستخدم {uid}"
        text += f"{idx}. {name}: {pts} نقطة\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل النصية (الإجابات)"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # التحقق من وجود لعبة نشطة في هذه المجموعة
    game = active_games.get(chat_id)
    if not game or game["status"] != "playing":
        return

    # التحقق من وجود سؤال حالي
    if "current_q_index" not in game or game["current_q_index"] >= len(game["questions"]):
        return

    question = game["questions"][game["current_q_index"]]

    # التأكد من أن المستخدم لم يجب على هذا السؤال مسبقاً
    if user_id in game.get("answered_users", set()):
        # رد خاص إذا أرسل إجابة مكررة (اختياري)
        # await update.message.reply_text("لقد أجبت بالفعل على هذا السؤال.")
        return

    # التحقق من صحة الإجابة
    correct = False
    if question["type"] == "choice":
        # إجابة اختيار من متعدد: نتوقع رقماً
        if text.isdigit():
            choice_index = int(text) - 1  # تحويل إلى 0-based
            if choice_index == question["correct_index"]:
                correct = True
    else:  # riddle
        # إجابة نصية (نتجاهل حالة الأحرف والمسافات الزائدة)
        if text.strip().lower() == question["answer"].lower():
            correct = True

    if correct:
        # تسجيل الإجابة
        game["answered_users"].add(user_id)

        # إذا كانت أول إجابة صحيحة
        if not game.get("correct_answer_given"):
            game["correct_answer_given"] = True
            # زيادة نقاط اللاعب
            game["players"][user_id] = game["players"].get(user_id, 0) + 1

            # إعلام المجموعة بالفائز
            first_name = update.effective_user.first_name
            await context.bot.send_message(
                chat_id,
                f"✅ {first_name} كان الأسرع! (+1 نقطة)"
            )
        else:
            # إجابة صحيحة ولكن بعد الفائز
            await update.message.reply_text("✅ إجابة صحيحة، ولكن هناك من سبقك!")
    else:
        # إجابة خاطئة - يمكن إرسال رد فوري (اختياري)
        # await update.message.reply_text("❌ إجابة خاطئة!")
        pass

# ======================== تشغيل البوت ========================

def main():
    if not TOKEN:
        logger.error("لم يتم تعيين BOT_TOKEN في متغيرات البيئة.")
        return

    # إنشاء التطبيق
    application = Application.builder().token(TOKEN).build()

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("endgame", endgame))
    application.add_handler(CommandHandler("score", score))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # بدء البوت
    logger.info("البوت يعمل...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
