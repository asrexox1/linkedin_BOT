import asyncio
import re
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
import aiosqlite

# ========== التهيئة ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # سيتم إضافته من Render

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found! Add it in Render environment variables")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== قاعدة البيانات ==========
DB_PATH = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                full_name TEXT,
                current_post_link TEXT,
                has_pending_interaction INTEGER DEFAULT 0,
                pending_from_user_id TEXT,
                pending_post_link TEXT,
                rating_positive INTEGER DEFAULT 0,
                rating_negative INTEGER DEFAULT 0,
                ignore_count INTEGER DEFAULT 0,
                lie_count INTEGER DEFAULT 0,
                frozen INTEGER DEFAULT 0,
                frozen_until TEXT
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS interaction_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id TEXT,
                requester_post TEXT,
                responder_id TEXT,
                responder_post TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        await db.commit()

# ========== دوال مساعدة ==========
def is_valid_linkedin_url(url: str) -> bool:
    patterns = [
        r'https?://(www\.)?linkedin\.com/(posts|feed)/',
        r'https?://(www\.)?linkedin\.com/.*/posts/'
    ]
    return any(re.match(p, url) for p in patterns)

def has_single_url(text: str) -> Tuple[bool, Optional[str]]:
    urls = re.findall(r'https?://[^\s]+', text)
    if len(urls) == 1 and is_valid_linkedin_url(urls[0]):
        return True, urls[0]
    return False, None

async def get_user(chat_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)) as cursor:
            return await cursor.fetchone()

async def update_user(chat_id: str, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in kwargs.items():
            await db.execute(f"UPDATE users SET {key} = ? WHERE chat_id = ?", (value, chat_id))
        await db.commit()

async def find_post_for_interaction(chat_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        query = '''
            SELECT chat_id, full_name, current_post_link, rating_positive, rating_negative
            FROM users 
            WHERE has_pending_interaction = 1 
            AND frozen = 0 
            AND chat_id != ?
            ORDER BY (rating_positive - rating_negative) DESC, rating_positive DESC
            LIMIT 1
        '''
        async with db.execute(query, (chat_id,)) as cursor:
            return await cursor.fetchone()

# ========== لوحات المفاتيح ==========
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🔍 طلب رابط للتفاعل"))
    builder.add(KeyboardButton(text="📝 اضافة بوستي"))
    builder.add(KeyboardButton(text="❌ حذف بوستي"))
    builder.add(KeyboardButton(text="👤 ملفي الشخصي"))
    builder.add(KeyboardButton(text="📊 تقييمي"))
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

# ========== حالات FSM ==========
class RegisterState(StatesGroup):
    waiting_for_name = State()

class AddPostState(StatesGroup):
    waiting_for_post = State()

# ========== الأوامر والأزرار ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    chat_id = str(message.chat.id)
    user = await get_user(chat_id)
    
    if user:
        await message.answer(f"✨ مرحباً بك مرة أخرى {user[1]}!", reply_markup=get_main_keyboard())
    else:
        await message.answer("👋 مرحباً! من فضلك أرسل اسمك الكامل (يفضل نفس اسمك على لينكد إن)")
        await state.set_state(RegisterState.waiting_for_name)

@dp.message(RegisterState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("❌ الرجاء إدخال اسم صحيح (على الأقل حرفين)")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (chat_id, full_name) VALUES (?, ?)",
            (str(message.chat.id), name)
        )
        await db.commit()
    
    await message.answer(f"✅ تم التسجيل بنجاح! أهلاً بك {name}", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(lambda m: m.text == "📝 اضافة بوستي")
async def add_post(message: types.Message, state: FSMContext):
    chat_id = str(message.chat.id)
    user = await get_user(chat_id)
    
    if not user:
        await message.answer("❌ الرجاء التسجيل أولاً عبر /start")
        return
    
    if user[7]:  # frozen
        frozen_until = datetime.fromisoformat(user[8])
        if frozen_until > datetime.now():
            await message.answer(f"❄️ حسابك مجمد حتى {frozen_until.strftime('%Y-%m-%d %H:%M')}")
            return
        else:
            await update_user(chat_id, frozen=0, frozen_until=None)
    
    await message.answer("📎 أرسل رابط بوست لينكد إن (رابط واحد فقط)")
    await state.set_state(AddPostState.waiting_for_post)

@dp.message(AddPostState.waiting_for_post)
async def process_post(message: types.Message, state: FSMContext):
    chat_id = str(message.chat.id)
    is_valid, url = has_single_url(message.text)
    
    if not is_valid:
        await message.answer("❌ رابط غير صالح! تأكد من:\n• رابط واحد فقط\n• رابط من linkedin.com يحتوي على /posts/ أو /feed/")
        return
    
    await update_user(chat_id, current_post_link=url, has_pending_interaction=1)
    await message.answer(f"✅ تم حفظ بوستك بنجاح!\n{url}\n\nسيراه المستخدمون عند طلبهم رابطاً للتفاعل", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(lambda m: m.text == "🔍 طلب رابط للتفاعل")
async def request_link(message: types.Message):
    chat_id = str(message.chat.id)
    user = await get_user(chat_id)
    
    if not user:
        await message.answer("❌ الرجاء التسجيل أولاً عبر /start")
        return
    
    if user[7]:  # frozen
        frozen_until = datetime.fromisoformat(user[8])
        if frozen_until > datetime.now():
            await message.answer(f"❄️ حسابك مجمد حتى {frozen_until.strftime('%Y-%m-%d %H:%M')}")
            return
    
    if user[3]:  # has_pending_interaction
        await message.answer("⚠️ لديك بوست معلق ينتظر التفاعلات! استخدم ❌ حذف بوستي أولاً")
        return
    
    target = await find_post_for_interaction(chat_id)
    if not target:
        await message.answer("📭 لا يوجد بوستات متاحة حالياً، حاول مرة أخرى لاحقاً")
        return
    
    target_id, target_name, target_post, pos_rating, neg_rating = target
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO interaction_requests (requester_id, requester_post, responder_id, responder_post) VALUES (?, ?, ?, ?)",
            (target_id, target_post, chat_id, user[2] or "")
        )
        await db.commit()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تم التفاعل", callback_data=f"confirm_interact_{target_id}")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_interact")]
    ])
    
    await message.answer(
        f"👤 الاسم: {target_name}\n⭐ التقييم: ✅ {pos_rating} | ❌ {neg_rating}\n🔗 الرابط: {target_post}\n\n"
        f"⚠️ بعد فتح الرابط والتفاعل، اضغط ✅ تم التفاعل",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

@dp.callback_query(lambda c: c.data.startswith("confirm_interact_"))
async def confirm_interaction(callback: types.CallbackQuery):
    target_id = callback.data.split("_")[2]
    responder_id = str(callback.from_user.id)
    
    await update_user(responder_id, has_pending_interaction=0)
    
    post_link = callback.message.text.split("🔗 الرابط: ")[1].split("\n")[0]
    await update_user(target_id, 
                     has_pending_interaction=0, 
                     pending_from_user_id=responder_id,
                     pending_post_link=post_link)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تم الرد", callback_data=f"replied_{responder_id}")],
        [InlineKeyboardButton(text="✅ صادق", callback_data=f"rate_honest_{responder_id}"),
         InlineKeyboardButton(text="❌ كاذب", callback_data=f"rate_liar_{responder_id}")]
    ])
    
    await bot.send_message(
        target_id,
        f"✅ {callback.from_user.full_name} تفاعل مع بوستك! من فضلك رد التفاعل على بوسته:\n{post_link}\n\nبعد الرد، اضغط على الأزرار أدناه:",
        reply_markup=keyboard
    )
    
    await callback.message.edit_text("✅ تم تأكيد تفاعلك! في انتظار رد الطرف الآخر...")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_interact")
async def cancel_interaction(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ تم إلغاء طلب التفاعل")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("replied_"))
async def mark_as_replied(callback: types.CallbackQuery):
    responder_id = callback.data.split("_")[1]
    requester_id = str(callback.from_user.id)
    
    await update_user(requester_id, pending_from_user_id=None, pending_post_link=None)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE interaction_requests SET status = 'completed' WHERE requester_id = ? AND responder_id = ?",
            (requester_id, responder_id)
        )
        await db.commit()
    
    await callback.message.edit_text("✅ تم تأكيد ردك! شكراً لمشاركتك")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("rate_"))
async def rate_user(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    rate_type, target_id = parts[1], parts[2]
    rater_id = str(callback.from_user.id)
    
    async def get_rating_val(chat_id, rating_type):
        user = await get_user(chat_id)
        return user[4] if rating_type == 'pos' else user[5]
    
    if rate_type == "honest":
        current = await get_rating_val(target_id, 'pos')
        await update_user(target_id, rating_positive=current + 1)
        await callback.message.answer("✅ تم تقييم المستخدم كصادق!")
    else:
        user = await get_user(target_id)
        new_lie_count = (user[8] or 0) + 1
        current_neg = await get_rating_val(target_id, 'neg')
        await update_user(target_id, rating_negative=current_neg + 1, lie_count=new_lie_count)
        
        if new_lie_count >= 4:
            frozen_until = datetime.now() + timedelta(hours=48)
            await update_user(target_id, frozen=1, frozen_until=frozen_until.isoformat())
            await bot.send_message(target_id, f"⚠️ تم تجميد حسابك 48 ساعة بسبب 4 مخالفات كذب! التجميد ينتهي {frozen_until.strftime('%Y-%m-%d %H:%M')}")
        else:
            await bot.send_message(target_id, f"⚠️ تم تقييمك ككاذب! أعد التفاعل مع بوست المستخدم بشكل صحيح (فرصة {new_lie_count}/4)")
        
        await callback.message.answer("❌ تم تقييم المستخدم ككاذب!")
    
    await callback.answer()

@dp.message(lambda m: m.text == "👤 ملفي الشخصي")
async def show_profile(message: types.Message):
    user = await get_user(str(message.chat.id))
    if not user:
        await message.answer("❌ الرجاء التسجيل أولاً")
        return
    
    status = "❄️ مجمد" if user[7] else "✅ نشط"
    text = (f"👤 الاسم: {user[1]}\n"
            f"📝 البوست: {user[2] or 'لا يوجد'}\n"
            f"⭐ التقييم: ✅ {user[4]} | ❌ {user[5]}\n"
            f"🚨 تجاهل: {user[6]} | كذب: {user[8] or 0}\n"
            f"📊 الحالة: {status}")
    await message.answer(text, disable_web_page_preview=True)

@dp.message(lambda m: m.text == "📊 تقييمي")
async def show_rating(message: types.Message):
    user = await get_user(str(message.chat.id))
    if user:
        await message.answer(f"📊 تقييمك:\n✅ صادق: {user[4]}\n❌ كاذب: {user[5]}\n🚫 تجاهل: {user[6]}\n🎭 كذب: {user[8] or 0}")

@dp.message(lambda m: m.text == "❌ حذف بوستي")
async def delete_post(message: types.Message):
    await update_user(str(message.chat.id), current_post_link=None, has_pending_interaction=0)
    await message.answer("✅ تم حذف بوستك بنجاح")

@dp.message(Command("changename"))
async def change_name(message: types.Message, state: FSMContext):
    await message.answer("✏️ أرسل اسمك الجديد")
    await state.set_state(RegisterState.waiting_for_name)

@dp.message()
async def handle_unknown(message: types.Message):
    await message.answer("❓ أمر غير معروف. استخدم الأزرار أو /start", reply_markup=get_main_keyboard())

# ========== تشغيل البوت ==========
async def main():
    await init_db()
    print("🚀 Bot is running on Render...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
