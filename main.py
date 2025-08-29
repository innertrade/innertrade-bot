import os, json, time, re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# -----------------------------
# Env
# -----------------------------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")
PUBLIC_URL         = os.getenv("PUBLIC_URL", "")            # e.g. https://innertrade-bot.onrender.com
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OFFSCRIPT_ENABLED  = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"  # GPT над сценарием
ALLOW_SETWEBHOOK   = os.getenv("ALLOW_SETWEBHOOK", "true").lower() == "true"

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not DATABASE_URL:      raise RuntimeError("DATABASE_URL missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

# -----------------------------
# DB
# -----------------------------
engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def db_exec(sql: str, params: dict = None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def load_state(uid: int) -> Dict[str, Any]:
    row = db_exec("""
        SELECT intent, step, COALESCE(data,'{}'::jsonb) AS data
        FROM user_state WHERE user_id=:uid
    """, {"uid": uid}).mappings().first()
    if not row:
        db_exec("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT DO NOTHING", {"uid": uid})
        db_exec("""
            INSERT INTO user_state(user_id,intent,step,data)
            VALUES (:uid,'greet','ask_form','{}'::jsonb)
            ON CONFLICT (user_id) DO NOTHING
        """, {"uid": uid})
        return {"intent": "greet", "step": "ask_form", "data": {}}
    return {"intent": row["intent"], "step": row["step"], "data": row["data"]}

def save_state(uid: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[dict]=None):
    cur = load_state(uid)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    if data   is None: data   = cur["data"]
    db_exec("""
        UPDATE user_state
           SET intent=:intent, step=:step, data=:data, updated_at=now()
         WHERE user_id=:uid
    """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data)})

def set_data(uid:int, patch:dict):
    st = load_state(uid)
    st["data"].update(patch or {})
    save_state(uid, data=st["data"])

# -----------------------------
# OpenAI (мягкий офф-скрипт)
# -----------------------------
def call_gpt(messages, sys_prompt:str) -> str:
    """
    Безопасный вызов OpenAI. Если ключа нет или будут ошибки — возвращаем пустую строку,
    бот продолжит по «ручной» логике.
    """
    if not OPENAI_API_KEY or not OFFSCRIPT_ENABLED:
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        # Универсальный вызов с Responses API; fallback на chat.completions
        try:
            resp = client.responses.create(
                model=OPENAI_MODEL,
                input=[{"role":"system","content":sys_prompt}] + messages,
                temperature=0.3,
            )
            # responses API
            content = ""
            if resp.output_text:
                content = resp.output_text
            else:
                # safety fallback
                content = json.dumps(resp.to_dict(), ensure_ascii=False)
            return content.strip()
        except Exception:
            comp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role":"system","content":sys_prompt}] + messages,
                temperature=0.3,
            )
            return comp.choices[0].message.content.strip()
    except Exception:
        return ""

# -----------------------------
# Текстовые шаблоны
# -----------------------------
def T(data:dict, you:str="ты"):
    """Мини «локализация» под ты/вы."""
    return {
        "greet_ask_form": f"👋 Привет! Можем просто поговорить — напиши, что болит в торговле.\n\nКак удобнее обращаться — **ты** или **вы**? (напиши одно слово)",
        "ask_name":       f"Как тебя зовут? (можно ник)",
        "set_form_ok":    f"Принято ({you}). Расскажи, что сейчас болит, или выбери пункт ниже.",
        "menu_hint":      f"Можем пойти по шагам позже — сейчас просто расскажи, что происходит в сделках.",
        "confirm_error":  f"Зафиксирую так: *{{err}}*\nПодходит?",
        "ask_context":    f"КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
        "ask_emotions":   f"ЭМОЦИИ. Что чувствуешь в такие моменты? Несколько слов.",
        "ask_thoughts":   f"МЫСЛИ. Что говоришь себе? 1–2 фразы, цитатами.",
        "ask_behavior":   f"ПОВЕДЕНИЕ. Что именно делаешь? Опиши действие глаголами.",
        "mer_done":       f"Ок, картину вижу. Сформулируем желаемое новое поведение одним предложением?",
        "ask_goal":       f"Сформулируй желаемое поведение (что делать вместо старого). Одним предложением.",
        "tote_ops":       f"TOTE/ОПЕРАЦИИ. Какие 2–3 шага помогут держаться цели в ближайших 3 сделках?",
        "tote_check":     f"TOTE/ПРОВЕРКА. Как поймёшь, что получилось держаться цели? (критерий) ",
        "tote_exit":      f"TOTE/ВЫХОД. Если получилось — что закрепим? Если нет — что меняем в шагах?",
        "done_lesson":    f"Готово. Мы сохранили результат. Можно вернуться к свободному разговору или продолжить.",
        "ok":             f"Ок, понял.",
        "not_understood": f"Я понял не всё. Можешь переформулировать коротко?",
    }

def keyboard_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🚑 У меня ошибка"), KeyboardButton("📄 Паспорт"))
    kb.add(KeyboardButton("🗒 Панель недели"), KeyboardButton("🆘 Экстренно"))
    return kb

def yes_no_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Да"), KeyboardButton("Нет"))
    return kb

# -----------------------------
# Flask + TeleBot
# -----------------------------
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False, num_threads=1, skip_pending=True)

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.get("/status")
def status_http():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK"

# -----------------------------
# Базовые команды
# -----------------------------
@bot.message_handler(commands=["ping"])
def ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def status_cmd(m):
    st = load_state(m.from_user.id)
    bot.reply_to(m, json.dumps({
        "ok": True,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok"
    }, ensure_ascii=False, indent=2))

@bot.message_handler(commands=["reset", "start"])
def reset(m):
    save_state(m.from_user.id, intent="greet", step="ask_form", data={"formality": None, "name": None})
    bot.send_message(m.chat.id, T({})["greet_ask_form"], reply_markup=keyboard_menu(), parse_mode="Markdown")

# -----------------------------
# Вспомогательное
# -----------------------------
ERROR_PATTERNS = [
    r"вхож[ую]|захож[ую]",
    r"двиг(аю|ать)\s*стоп",
    r"закрыва(ю|ть)\s*(раньше|на.*коррекц| по первой)",
    r"усредня",
    r"наруша(ю|ть)\s*правил",
    r"безубыток|перетаскив(аю|ать)",
]

def looks_like_behavior_error(text:str)->bool:
    t = text.lower()
    return any(re.search(p, t) for p in ERROR_PATTERNS)

def summarize_error_free(texts:list[str])->str:
    joined = " ".join(texts)[-800:]
    # мини перефраз без GPT
    return re.sub(r"\s+", " ", joined).strip()

# -----------------------------
# Основной обработчик сообщений
# -----------------------------
@bot.message_handler(content_types=["text"])
def all_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()

    st = load_state(uid)
    you = (st["data"].get("formality") or "ты")
    tr = T(st["data"], you=you)

    # 0) ветка формальности и имени
    if st["intent"] == "greet":
        if st["step"] == "ask_form":
            low = txt.lower()
            if low in ("ты","вы"):
                set_data(uid, {"formality": low})
                save_state(uid, intent="free", step="free_talk")
                bot.send_message(m.chat.id, tr["set_form_ok"], reply_markup=keyboard_menu(), parse_mode="Markdown")
                return
            else:
                # если сразу начал по делу — не блокируем
                if looks_like_behavior_error(txt):
                    # уже конкретика — подтверждение
                    err = txt
                    set_data(uid, {"pending_error": err})
                    save_state(uid, intent="confirm_error", step="ask_confirm")
                    bot.send_message(m.chat.id, tr["confirm_error"].format(err=err), reply_markup=yes_no_kb(), parse_mode="Markdown")
                    return
                # иначе спросим формальность ещё раз, но мягко
                bot.send_message(m.chat.id, tr["greet_ask_form"], reply_markup=keyboard_menu(), parse_mode="Markdown")
                return

    # 1) подтверждение ошибки (без «нажимай кнопку»)
    if st["intent"] == "confirm_error":
        if st["step"] == "ask_confirm":
            if txt.lower() in ("да","ok","ок","подходит","угу","верно","правильно"):
                # старт MERCEDES
                save_state(uid, intent="mercedes", step="ask_context")
                bot.send_message(m.chat.id, tr["ask_context"])
                return
            if txt.lower() in ("нет","не","не подходит"):
                # сброс pending_error
                set_data(uid, {"pending_error": None})
                save_state(uid, intent="free", step="free_talk")
                bot.send_message(m.chat.id, tr["not_understood"])
                return
            # если вместо да/нет прислал подробности — считаем это уточнением ошибки → снова спросим «подходит?»
            pend = (st["data"].get("pending_error") or "")
            merged = (pend + ". " + txt).strip()
            set_data(uid, {"pending_error": merged})
            bot.send_message(m.chat.id, tr["confirm_error"].format(err=merged), reply_markup=yes_no_kb(), parse_mode="Markdown")
            return

    # 2) MERCEDES поток (короткая версия)
    if st["intent"] == "mercedes":
        data = st["data"]
        mer = data.get("mer", {})

        if st["step"] == "ask_context":
            mer["context"] = txt
            set_data(uid, {"mer": mer})
            save_state(uid, intent="mercedes", step="ask_emotions")
            bot.send_message(m.chat.id, tr["ask_emotions"])
            return

        if st["step"] == "ask_emotions":
            mer["emotions"] = txt
            set_data(uid, {"mer": mer})
            save_state(uid, intent="mercedes", step="ask_thoughts")
            bot.send_message(m.chat.id, tr["ask_thoughts"])
            return

        if st["step"] == "ask_thoughts":
            mer["thoughts"] = txt
            set_data(uid, {"mer": mer})
            save_state(uid, intent="mercedes", step="ask_behavior")
            bot.send_message(m.chat.id, tr["ask_behavior"])
            return

        if st["step"] == "ask_behavior":
            mer["behavior"] = txt
            set_data(uid, {"mer": mer})
            save_state(uid, intent="tote", step="ask_goal")
            bot.send_message(m.chat.id, tr["ask_goal"])
            return

    # 3) TOTE
    if st["intent"] == "tote":
        data = st["data"]
        tote = data.get("tote", {})

        if st["step"] == "ask_goal":
            tote["goal"] = txt
            set_data(uid, {"tote": tote})
            save_state(uid, intent="tote", step="ask_ops")
            bot.send_message(m.chat.id, tr["tote_ops"])
            return

        if st["step"] == "ask_ops":
            tote["ops"] = txt
            set_data(uid, {"tote": tote})
            save_state(uid, intent="tote", step="ask_check")
            bot.send_message(m.chat.id, tr["tote_check"])
            return

        if st["step"] == "ask_check":
            tote["check"] = txt
            set_data(uid, {"tote": tote})
            save_state(uid, intent="tote", step="ask_exit")
            bot.send_message(m.chat.id, tr["tote_exit"])
            return

        if st["step"] == "ask_exit":
            tote["exit"] = txt
            set_data(uid, {"tote": tote})
            # (MVP) здесь можно сохранить в таблицу errors (опустим SQL для краткости)
            save_state(uid, intent="free", step="free_talk")
            bot.send_message(m.chat.id, tr["done_lesson"], reply_markup=keyboard_menu())
            return

    # 4) Свободный разговор с GPT «сверху» (мягкий коучинг)
    #    Если видим поведенческую ошибку — сами формируем подтверждение и входим в MERCEDES.
    if looks_like_behavior_error(txt):
        err = txt
        set_data(uid, {"pending_error": err})
        save_state(uid, intent="confirm_error", step="ask_confirm")
        bot.send_message(m.chat.id, T(st["data"], you=you)["confirm_error"].format(err=err),
                         reply_markup=yes_no_kb(), parse_mode="Markdown")
        return

    # GPT помогает вести естественный диалог (если включён)
    sys_prompt = (
        "Ты мягкий наставник-трейдинга. Общайся естественно, короткими сообщениями. "
        "Если в сообщении уже есть конкретная поведенческая проблема (вроде: «двигаю стоп», "
        "«усредняюсь», «вхожу до сигнала») — перефразируй и попроси подтверждение «Подходит?» "
        "и НИЧЕГО не предлагай нажимать. Если человек подтверждает — переводим в пошаговый разбор "
        "по схеме MERCEDES (контекст→эмоции→мысли→поведение), затем цель и TOTE. "
        "Если ещё рано — задай 1–2 мягких вопроса, помогая докопаться до конкретного поведения. "
        "Избегай слов «нажми кнопку», «вернёмся к шагам курса». Не упоминай названия техник, пока не спросят."
    )
    answer = call_gpt(
        [{"role":"user","content": txt}],
        sys_prompt=sys_prompt
    ) or T(st["data"], you=you)["menu_hint"]

    bot.send_message(m.chat.id, answer, reply_markup=keyboard_menu(), parse_mode="Markdown")

# -----------------------------
# Вебхук установка при старте (опционально)
# -----------------------------
def ensure_webhook():
    if not ALLOW_SETWEBHOOK: return
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": f"{PUBLIC_URL}/{WEBHOOK_PATH}",
        "secret_token": TG_WEBHOOK_SECRET,
        "allowed_updates": ["message","callback_query"],
        "drop_pending_updates": False
    }
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception:
        pass

if __name__ == "__main__":
    ensure_webhook()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
