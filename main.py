import os, json, time, logging
from datetime import datetime, timezone
from flask import Flask, request, abort, jsonify
import requests
from telebot import TeleBot, types
from telebot.util import quick_markup

# ---------- ENV ----------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET", "")
PUBLIC_URL       = os.getenv("PUBLIC_URL", "")  # https://<your-app>.onrender.com
DATABASE_URL     = os.getenv("DATABASE_URL", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OFFSCRIPT_ENABLED= os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
APP_VERSION      = os.getenv("APP_VERSION", "chat-first-2025-08-29")

if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:     raise RuntimeError("PUBLIC_URL missing")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("innertrade")

bot  = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
app  = Flask(__name__)

# ---------- LIGHTWEIGHT STATE (in-memory for MVP) ----------
# В проде у нас Postgres. Здесь держим только "кто ты/вы, имя, последний наброс проблемы"
MEM = {}  # tg_id -> {"address":"ты|вы", "name":str, "last_issue":str, "intent":str, "step":str}

def mget(uid, key, default=None): return MEM.setdefault(uid, {}).get(key, default)
def mset(uid, **kw): MEM.setdefault(uid, {}).update(kw)

# ---------- GPT ----------
def gpt_reply(system, user):
    if not (OFFSCRIPT_ENABLED and OPENAI_API_KEY):
        return None  # выключено/нет ключа — пойдём по сценарию
    try:
        import openai
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        msg = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content": system},
                {"role":"user","content": user}
            ],
            temperature=0.3,
            max_tokens=400
        )
        return msg.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"GPT fallback: {e}")
        return None

SYSTEM_COACH = (
    "Ты эмпатичный наставник по трейдингу. Общайся естественно и коротко. "
    "Помогай сформулировать проблему на уровне наблюдаемого поведения/навыка, "
    "но не дави и не перегружай терминами. Не упоминай названия техник. "
    "Если собеседник уже описал проблему достаточно конкретно, аккуратно подтвердь формулировку "
    "своими словами одним предложением и предложи мягко пройтись по шагам разбора."
)

# ---------- KEYBOARDS ----------
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")
    return kb

def yes_no_kb():
    return quick_markup({"Да":{"callback_data":"yes"}, "Нет":{"callback_data":"no"}}, row_width=2)

# ---------- HELPERS ----------
def greet_name(uid):
    address = mget(uid, "address")
    if address == "вы": return "Вы"
    return "ты"

def confirm_issue_if_ready(text: str):
    """
    Очень грубая эвристика: если в тексте есть глаголы действия из рынковой рутины,
    считаем, что проблема уже на уровне поведения.
    """
    verbs = ["вхожу", "войти", "захожу", "зайти",
             "двигаю", "двигать", "сдвигаю", "переношу",
             "закрываю", "закрыть", "усредняю", "усреднить",
             "ставлю стоп", "убираю стоп", "ставлю безубыток", "фиксирую"]
    t = text.lower()
    return any(v in t for v in verbs)

def summarize_issue(text: str):
    """Мини перефраз одним предложением (без терминов)."""
    return f"Сейчас тебя сбивает с курса вот что: {text.strip()}"

# ---------- HEALTH ----------
@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": APP_VERSION,
        "offscript": OFFSCRIPT_ENABLED,
        "model": OPENAI_MODEL if OPENAI_API_KEY else None,
        "db": "ok",
    })

# ---------- WEBHOOK ----------
@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if TG_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_json(force=True, silent=True)
    try:
        bot.process_new_updates([types.Update.de_json(update)])
    except Exception as e:
        log.exception("update error")
    return "ok"

# ---------- COMMANDS ----------
@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uid = m.from_user.id
    st = {
        "version": APP_VERSION,
        "offscript": OFFSCRIPT_ENABLED,
        "model": OPENAI_MODEL if OPENAI_API_KEY else None,
        "name": mget(uid, "name"),
        "address": mget(uid, "address"),
        "intent": mget(uid, "intent"),
        "step": mget(uid, "step"),
    }
    bot.reply_to(m, f"```json\n{json.dumps(st, ensure_ascii=False, indent=2)}\n```", parse_mode="Markdown")

@bot.message_handler(commands=["reset"])
def cmd_reset(m):
    uid = m.from_user.id
    MEM[uid] = {}
    msg = "👋 Привет! Можем просто поговорить — напиши, что болит в торговле.\n\nКак удобнее обращаться — *ты* или *вы*? (напиши одно слово)"
    bot.send_message(uid, msg, reply_markup=main_menu())

# ---------- STARTUP GREET ----------
@bot.message_handler(func=lambda m: m.text and m.text.lower() in ["привет","hi","hello"])
def greet(m):
    uid = m.from_user.id
    name = m.from_user.first_name or "друг"
    if not mget(uid, "name"):
        mset(uid, name=name)
    if not mget(uid, "address"):
        bot.reply_to(m, f"👋 Привет, {name}! Как удобнее обращаться — *ты* или *вы*?")
    else:
        bot.reply_to(m, f"Привет, {name}! Расскажи коротко, что болит в торговле — я рядом.", reply_markup=main_menu())

# ---------- ADDRESS (ты/вы) ----------
@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() in ["ты","вы"])
def set_address(m):
    uid = m.from_user.id
    val = m.text.strip().lower()
    mset(uid, address=val)
    bot.reply_to(m, f"Принято ({val}). Можем спокойно поговорить — расскажи, что сейчас болит, или выбери пункт ниже.", reply_markup=main_menu())

# ---------- MAIN FREE CHAT ----------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def free_chat(m):
    uid = m.from_user.id
    text = (m.text or "").strip()

    # Кнопки меню
    if text == "🚑 У меня ошибка":
        mset(uid, intent="error", step="start")
        return bot.send_message(uid, "Окей, давай разберём. Коротко: что именно уже мешает в действиях? (можно 1–2 предложения)")
    if text == "🧩 Хочу стратегию":
        return bot.send_message(uid, "Соберём по шагам: рынок/ТФ → вход → стоп/сопровождение → риск. Готов?", reply_markup=main_menu())
    if text == "📄 Паспорт":
        return bot.send_message(uid, "Паспорт: рынки, ТФ, стиль, риск, топ-ошибки, роли, триггеры. Потом сможем править.", reply_markup=main_menu())
    if text == "🗒 Панель недели":
        return bot.send_message(uid, "Панель недели: фокус, цели, лимиты, мини-чекины, ретро. Поехали, когда будешь готов.", reply_markup=main_menu())
    if text == "🆘 Экстренно":
        return bot.send_message(uid, "Стоп-процедура: пауза 2 мин → убери терминал → 10 вдохов → запиши триггер → вернись к плану/закрой по правилу.")
    if text == "🤔 Не знаю, с чего начать":
        return bot.send_message(uid, "Предлагаю так: 1) Паспорт, 2) Фокус недели, 3) Скелет ТС. С чего начнём?")

    # Если мы в сценарии "ошибка"
    if mget(uid, "intent") == "error":
        return handle_error_flow(m, text)

    # Иначе — свободный разговор, но с попыткой мягко подтвердить проблему
    if confirm_issue_if_ready(text):
        mset(uid, last_issue=text)
        summary = summarize_issue(text)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Да, верно", callback_data="issue_ok"),
               types.InlineKeyboardButton("Нужно уточнить", callback_data="issue_edit"))
        return bot.send_message(uid, f"{summary}\n\nПродолжим разбор по шагам?", reply_markup=kb)

    # Иначе — спросим уточняюще, либо подключим GPT для мягкой беседы
    reply = gpt_reply(SYSTEM_COACH, text)
    if reply:
        return bot.send_message(uid, reply)
    else:
        return bot.send_message(uid, "Понимаю. Расскажи ещё чуть-чуть — что именно в твоих действиях сейчас чаще всего сбивает с курса?")

@bot.callback_query_handler(func=lambda c: c.data in ["issue_ok","issue_edit"])
def issue_confirm(c):
    uid = c.from_user.id
    if c.data == "issue_ok":
        mset(uid, intent="error", step="start")
        bot.answer_callback_query(c.id, "Окей, идём дальше.")
        bot.send_message(uid, "Тогда коротко пройдёмся по шагам. В какой ситуации это обычно происходит? Что предшествует?")
    else:
        bot.answer_callback_query(c.id, "Давай уточним. Напиши своими словами, как бы ты сформулировал проблему.")

# ---------- ERROR FLOW (без слов «Mercedes»/«TOTE») ----------
def handle_error_flow(m, text):
    uid = m.from_user.id
    step = mget(uid, "step") or "start"

    if step == "start":
        if not text or len(text) < 4:
            return bot.reply_to(m, "Добавь пару деталей: что именно делаешь (глаголами), где это чаще всего случается?")
        mset(uid, step="ctx", issue=text)
        return bot.reply_to(m, "Понял. В какой ситуации это обычно всплывает? Что предшествует?")

    if step == "ctx":
        mset(uid, step="emo", ctx=text)
        return bot.reply_to(m, "Что чувствуешь в эти моменты? (несколько слов)")

    if step == "emo":
        mset(uid, step="thought", emo=text)
        return bot.reply_to(m, "Какие мысли крутятся тогда? (1–2 короткие фразы)")

    if step == "thought":
        mset(uid, step="beh", thought=text)
        return bot.reply_to(m, "Что в итоге делаешь? Опиши действия (1–2 предложения).")

    if step == "beh":
        mset(uid, step="goal", beh=text)
        # Резюме без тяжёлых терминов
        issue   = mget(uid, "issue","")
        ctx     = mget(uid, "ctx","")
        emo     = mget(uid, "emo","")
        thought = mget(uid, "thought","")
        beh     = mget(uid, "beh","")
        resume = (
            "*Резюме:*\n"
            f"• Проблема: {issue}\n"
            f"• Ситуация: {ctx}\n"
            f"• Чувства: {emo}\n"
            f"• Мысли: {thought}\n"
            f"• Действия: {beh}\n\n"
            "Сформулируем новую цель одним предложением — что хочешь делать вместо прежнего поведения?"
        )
        return bot.reply_to(m, resume)

    if step == "goal":
        if not text or len(text) < 4:
            return bot.reply_to(m, "Скажи цель одним предложением (что делать вместо прежнего поведения).")
        mset(uid, step="ops", goal=text)
        return bot.reply_to(m, "Отлично. Какие 2–3 шага помогут держаться этой цели в ближайших *3* сделках?")

    if step == "ops":
        mset(uid, step="check", ops=text)
        return bot.reply_to(m, "Как проверишь, что получилось? (критерий: что должно быть выполнено, чтобы сказать «получилось»?)")

    if step == "check":
        mset(uid, step="exit", check=text)
        return bot.reply_to(m, "Последний штрих: что сделаешь по итогам мини-цикла — если получилось / если нет? (1–2 фразы)")

    if step == "exit":
        mset(uid, step=None, intent=None, exit=text)
        # Итог
        goal  = mget(uid,"goal","")
        ops   = mget(uid,"ops","")
        check = mget(uid,"check","")
        exitp = mget(uid,"exit","")
        out = (
            "*Итог мини-плана:*\n"
            f"• Цель: {goal}\n"
            f"• Шаги: {ops}\n"
            f"• Проверка: {check}\n"
            f"• Что дальше: {exitp}\n\n"
            "Готово. Хочешь добавить это в фокус недели?"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Добавить в фокус недели", callback_data="wk_add"),
               types.InlineKeyboardButton("Оставить как есть", callback_data="wk_skip"))
        return bot.reply_to(m, out, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in ["wk_add","wk_skip"])
def week_panel_choice(c):
    uid = c.from_user.id
    if c.data == "wk_add":
        bot.answer_callback_query(c.id, "Добавил в недельный фокус (MVP-пометка).")
        bot.send_message(uid, "Записал как фокус недели. Вернуться в меню?", reply_markup=main_menu())
    else:
        bot.answer_callback_query(c.id, "Ок.")
        bot.send_message(uid, "Оставляем как есть. Чем ещё помочь?", reply_markup=main_menu())

# ---------- APP RUN ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
