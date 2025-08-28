# main.py
import os, json, logging, time, datetime as dt
from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# -------------------- ЛОГИ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# -------------------- ENV ---------------------
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
DATABASE_URL        = os.getenv("DATABASE_URL")
PUBLIC_URL          = os.getenv("PUBLIC_URL")            # https://innertrade-bot.onrender.com
WEBHOOK_PATH        = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_WEBHOOK_SECRET   = os.getenv("TG_WEBHOOK_SECRET")     # любой длинный случайный
ALLOW_SET_WEBHOOK   = os.getenv("ALLOW_SET_WEBHOOK", "1") in ("1", "true", "True")
MODEL_SMALL         = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

# -------------------- OPENAI ------------------
oa = OpenAI(api_key=OPENAI_API_KEY)

def gpt_short(system, user, max_tokens=300, temperature=0.2):
    try:
        resp = oa.chat.completions.create(
            model=MODEL_SMALL,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"OpenAI error: {e}")
        return None

# -------------------- DB ----------------------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            # минимальная схема
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users(
              user_id BIGINT PRIMARY KEY,
              name TEXT,
              username TEXT,
              address TEXT,              -- 'tu' | 'vy'
              mode TEXT NOT NULL DEFAULT 'course',
              created_at TIMESTAMPTZ DEFAULT now(),
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS user_state(
              user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent TEXT,
              step   TEXT,
              data   JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS errors(
              id BIGSERIAL PRIMARY KEY,
              user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
              error_text TEXT NOT NULL,
              pattern_behavior TEXT,
              pattern_emotion  TEXT,
              pattern_thought  TEXT,
              positive_goal    TEXT,
              tote_goal  TEXT,
              tote_ops   TEXT,
              tote_check TEXT,
              tote_exit  TEXT,
              created_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            log.info("DB connected & migrated")
    except OperationalError as e:
        log.warning(f"DB not available yet: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB (stateless)")

def db_exec(sql, params=None, fetch=False, one=False):
    if not engine:
        return None
    with engine.begin() as conn:
        res = conn.execute(text(sql), params or {})
        if fetch:
            rows = res.mappings().all()
            return rows[0] if (one and rows) else rows

def upsert_user(u):
    if not engine: return
    db_exec("""
    INSERT INTO users(user_id,name,username)
    VALUES (:id,:name,:username)
    ON CONFLICT (user_id) DO UPDATE
      SET name=EXCLUDED.name, username=EXCLUDED.username, updated_at=now()
    """, {"id": u.id, "name": f"{getattr(u,'first_name', '')} {getattr(u,'last_name','')}".strip(),
          "username": getattr(u,'username',None)})

def get_state(uid):
    if not engine: return {"intent":"greet","step":"ask_form","data":{}}
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid",
                  {"uid": uid}, fetch=True, one=True)
    if row:
        return {"intent": row["intent"], "step": row["step"], "data": row["data"] or {}}
    return {"intent":"greet","step":"ask_form","data":{}}

def save_state(uid, intent=None, step=None, data_patch=None):
    if not engine: return
    st = get_state(uid)
    if data_patch:
        st["data"] = {**(st.get("data") or {}), **data_patch}
    if intent: st["intent"]=intent
    if step:   st["step"]=step
    db_exec("""
    INSERT INTO user_state(user_id,intent,step,data)
    VALUES (:uid,:intent,:step,:data)
    ON CONFLICT (user_id) DO UPDATE
      SET intent=:intent, step=:step, data=:data, updated_at=now()
    """, {"uid": uid, "intent": st["intent"], "step": st["step"],
          "data": json.dumps(st["data"])})

def insert_error(uid, payload):
    if not engine: return
    db_exec("""
      INSERT INTO errors(user_id,error_text,pattern_behavior,pattern_emotion,pattern_thought,
                         positive_goal,tote_goal,tote_ops,tote_check,tote_exit)
      VALUES (:uid,:e,:pb,:pe,:pt,:pg,:tg,:to,:tc,:te)
    """, { "uid": uid,
           "e": payload.get("error_text"),
           "pb":payload.get("pattern_behavior"),
           "pe":payload.get("pattern_emotion"),
           "pt":payload.get("pattern_thought"),
           "pg":payload.get("positive_goal"),
           "tg":payload.get("tote_goal"),
           "to":payload.get("tote_ops"),
           "tc":payload.get("tote_check"),
           "te":payload.get("tote_exit") })

# -------------------- TG BOT ------------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def addr_label(uid):
    st = get_state(uid)
    return "ты" if (st["data"].get("address")=="tu") else ("вы" if st["data"].get("address")=="vy" else None)

def greet_text(name, addr):
    hello = f"👋 Привет, {name}!" if name else "👋 Привет!"
    tail = "Можем спокойно поговорить — просто напиши, что болит в торговле. Или выбери пункт ниже."
    return f"{hello} {tail}"

# -------------------- DIALOG HELPERS ----------
def ask_address(chat_id, uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("ты", callback_data="addr_tu"),
           types.InlineKeyboardButton("вы", callback_data="addr_vy"))
    bot.send_message(chat_id, "Как удобнее общаться — *ты* или *вы*?", reply_markup=kb)
    save_state(uid, intent="greet", step="ask_address")

def propose_error_summary(uid, user_text):
    """Мягкая конкретизация проблемы GPT'ом (уровень поведения/навыка), с подтверждением."""
    system = ("Ты коуч по трейдингу. Суммируй проблему в 1–2 коротких предложения "
              "на уровне наблюдаемого поведения/навыка, без морали. Русский язык.")
    draft = gpt_short(system, user_text, max_tokens=120) or user_text.strip()
    return draft

def ask_mercedes_block(chat_id, uid, block):
    labels = {
        "context":"КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
        "emotion":"ЭМОЦИИ. Что чувствуешь в момент ошибки? (несколько слов)",
        "thought":"МЫСЛИ. Что говоришь себе тогда? (1–2 фразы, цитатами)",
        "behavior":"ПОВЕДЕНИЕ. Что делаешь конкретно? (глаголами, 1–2 предложения)",
    }
    bot.send_message(chat_id, labels[block], reply_markup=main_menu())
    save_state(uid, step=f"mer_{block}")

def build_status(uid):
    st = get_state(uid)
    return {
        "ok": True,
        "time": dt.datetime.utcnow().replace(microsecond=0).isoformat()+"Z",
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok" if engine else "no-db"
    }

# -------------------- COMMANDS ----------------
@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    status = build_status(m.from_user.id)
    bot.send_message(m.chat.id, "```\n"+json.dumps(status, ensure_ascii=False, indent=2)+"\n```", parse_mode="Markdown")

@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    upsert_user(m.from_user)
    save_state(m.from_user.id, intent="greet", step="ask_form", data_patch={})
    # сразу предлагаем выбор "ты/вы" один раз, затем — свободный вход
    addr = addr_label(m.from_user.id)
    bot.send_message(m.chat.id, greet_text(m.from_user.first_name, addr), reply_markup=main_menu())
    if not addr:
        ask_address(m.chat.id, m.from_user.id)

# -------------------- INLINE (ты/вы) ----------
@bot.callback_query_handler(func=lambda c: c.data in ("addr_tu","addr_vy"))
def cb_address(c):
    uid = c.from_user.id
    choice = "tu" if c.data=="addr_tu" else "vy"
    save_state(uid, data_patch={"address": choice})
    bot.answer_callback_query(c.id, "Ок")
    bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    bot.send_message(c.message.chat.id, "Принято. Можем просто поговорить — расскажи, что сейчас болит, или выбери пункт ниже.", reply_markup=main_menu())
    save_state(uid, intent="greet", step="free_talk")

# -------------------- BUTTON INTENTS ----------
@bot.message_handler(func=lambda msg: msg.text=="🚑 У меня ошибка")
def intent_error_btn(m): return intent_error(m)

def intent_error(m):
    uid = m.from_user.id
    save_state(uid, intent="error", step="ask_error", data_patch={"err_buffer":[]})
    bot.send_message(m.chat.id,
        "Опиши свою текущую трудность 1–2 предложениями (как ты это делаешь в реальности).", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🧩 Хочу стратегию")
def intent_strategy(m):
    save_state(m.from_user.id, "strategy", step="intro")
    bot.send_message(m.chat.id,
        "Ок, соберём ТС по конструктору:\n1) Подход/ТФ/вход\n2) Стоп/сопровождение/выход/риск\nГотов?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="📄 Паспорт")
def intent_passport(m):
    save_state(m.from_user.id, "passport", step="intro")
    bot.send_message(m.chat.id,"Паспорт трейдера. Начнём с рынков/инструментов?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🗒 Панель недели")
def intent_week_panel(m):
    save_state(m.from_user.id, "week_panel", step="intro")
    bot.send_message(m.chat.id,"Панель недели: фокус, 1–2 цели, лимиты, короткие чек-ины. С чего начнём?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🆘 Экстренно: поплыл")
def intent_panic(m):
    save_state(m.from_user.id, "panic", step="protocol")
    bot.send_message(m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану или закрой позицию по правилу",
        reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🤔 Не знаю, с чего начать")
def intent_start_help(m):
    save_state(m.from_user.id, "start_help", step="intro")
    bot.send_message(m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\nС чего начнём?",
        reply_markup=main_menu())

# -------------------- CORE DIALOG -------------
@bot.message_handler(content_types=["text"])
def any_text(m):
    uid = m.from_user.id
    upsert_user(m.from_user)
    st = get_state(uid)
    text_in = (m.text or "").strip()

    # быстрые маршруты по словам
    low = text_in.lower()
    if st["intent"]=="greet" and st["step"] in ("ask_form","free_talk"):
        # свободное общение: если явно просит "разбор", стартуем ошибку
        if "разбор" in low or "ошибк" in low:
            return intent_error(m)
        # иначе — мягкая поддержка, 1 реплика + вопрос-оценка боли
        reply = gpt_short(
            "Ты эмпатичный коуч. Ответь кратко и естественно, 1–2 фразы, без советов, с переспрашиванием про суть запроса. Русский.",
            f"Пользователь пишет: {text_in}",
            max_tokens=120, temperature=0.4
        ) or "Понимаю. Можем разобрать это по шагам. Расскажи, что именно болит сейчас?"
        bot.send_message(m.chat.id, reply, reply_markup=main_menu())
        save_state(uid, step="free_talk")
        return

    # Ветвь "У меня ошибка"
    if st["intent"]=="error":
        data = st.get("data", {}) or {}
        buf = data.get("err_buffer", [])

        if st["step"]=="ask_error":
            # Копим 1–2 подхода, затем GPT даёт конкретизацию и просим подтвердить
            buf.append(text_in)
            save_state(uid, step="clarify_1", data_patch={"err_buffer":buf})
            if len(buf) == 1:
                bot.send_message(m.chat.id, "Понял. Если есть что добавить — напиши ещё одну деталь. Или скажи «готово».", reply_markup=main_menu())
                return
            # есть 2 подхода → делаем короткое резюме
            draft = propose_error_summary(uid, "\n".join(buf))
            save_state(uid, step="confirm_error", data_patch={"err_draft": draft})
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Да", callback_data="err_ok"),
                   types.InlineKeyboardButton("Не совсем", callback_data="err_more"))
            bot.send_message(m.chat.id, f"Зафиксирую так:\n> {draft}\nПодходит?", reply_markup=kb)
            return

        if st["step"]=="clarify_1":
            if low in ("готов","готово","да","ок","хватит"):
                draft = propose_error_summary(uid, "\n".join(buf))
                save_state(uid, step="confirm_error", data_patch={"err_draft": draft})
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("Да", callback_data="err_ok"),
                       types.InlineKeyboardButton("Не совсем", callback_data="err_more"))
                bot.send_message(m.chat.id, f"Зафиксирую так:\n> {draft}\nПодходит?", reply_markup=kb)
                return
            # добавляем ещё деталь и снова резюмируем
            buf.append(text_in)
            draft = propose_error_summary(uid, "\n".join(buf))
            save_state(uid, step="confirm_error", data_patch={"err_buffer":buf,"err_draft":draft})
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Да", callback_data="err_ok"),
                   types.InlineKeyboardButton("Не совсем", callback_data="err_more"))
            bot.send_message(m.chat.id, f"Зафиксирую так:\n> {draft}\nПодходит?", reply_markup=kb)
            return

        if st["step"]=="mer_context":
            save_state(uid, data_patch={"mer_context": text_in})
            return ask_mercedes_block(m.chat.id, uid, "emotion")
        if st["step"]=="mer_emotion":
            save_state(uid, data_patch={"mer_emotion": text_in})
            return ask_mercedes_block(m.chat.id, uid, "thought")
        if st["step"]=="mer_thought":
            save_state(uid, data_patch={"mer_thought": text_in})
            return ask_mercedes_block(m.chat.id, uid, "behavior")
        if st["step"]=="mer_behavior":
            save_state(uid, data_patch={"mer_behavior": text_in})
            # Резюме MERCEDES → запрос позитивной цели
            d = get_state(uid)["data"]
            summary = (f"Ок, вижу паттерн.\n"
                       f"Контекст: {d.get('mer_context','—')}\n"
                       f"Эмоции: {d.get('mer_emotion','—')}\n"
                       f"Мысли: {d.get('mer_thought','—')}\n"
                       f"Поведение: {d.get('mer_behavior','—')}\n")
            bot.send_message(m.chat.id, summary)
            bot.send_message(m.chat.id, "Сформулируем новую цель одним предложением: *что хочешь делать вместо прежнего поведения?*")
            save_state(uid, step="ask_goal")
            return

        if st["step"]=="ask_goal":
            save_state(uid, data_patch={"positive_goal": text_in})
            bot.send_message(m.chat.id, "Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках? (списком)")
            save_state(uid, step="ask_ops")
            return

        if st["step"]=="ask_ops":
            # финализация: пишем запись в errors
            d = get_state(uid)["data"]
            payload = {
                "error_text": d.get("err_draft") or "—",
                "pattern_behavior": d.get("mer_behavior"),
                "pattern_emotion":  d.get("mer_emotion"),
                "pattern_thought":  d.get("mer_thought"),
                "positive_goal":    d.get("positive_goal"),
                "tote_goal":        d.get("positive_goal"),
                "tote_ops":         text_in,
                "tote_check":       "Выполнил ли я шаги и остался в позиции до плана?",
                "tote_exit":        "Да — выхожу; Нет — вношу правку и повторяю"
            }
            insert_error(uid, payload)
            bot.send_message(m.chat.id, "Готово. Сохранил разбор ошибки и цель. Готов продолжать в удобном темпе.", reply_markup=main_menu())
            save_state(uid, intent="idle", step="done")
            return

    # Фолбэк
    bot.send_message(m.chat.id, "Принял. Можем поговорить здесь или выбери пункт в меню ниже.", reply_markup=main_menu())

# Подтверждение формулировки ошибки
@bot.callback_query_handler(func=lambda c: c.data in ("err_ok","err_more"))
def cb_err_confirm(c):
    uid = c.from_user.id
    st = get_state(uid)
    if c.data=="err_ok":
        bot.answer_callback_query(c.id, "Ок, идём дальше")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        ask_mercedes_block(c.message.chat.id, uid, "context")
        save_state(uid, step="mer_context")
    else:
        bot.answer_callback_query(c.id, "Сформулируй, что поправить/добавить")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        save_state(uid, step="clarify_1")

# -------------------- FLASK -------------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": dt.datetime.utcnow().isoformat()})

@app.get("/status")
def http_status():
    # HTTP-статус сервиса (не путать с /status в чате)
    return jsonify(build_status(0))

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Безопасность: проверяем секрет
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    # Телеграм должен получать 200 очень быстро
    try:
        update = types.Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
    except Exception as e:
        log.exception(f"webhook error: {e}")
    return "OK", 200

def ensure_webhook():
    if not ALLOW_SET_WEBHOOK: 
        log.info("Auto setWebhook disabled")
        return
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    data = {
        "url": f"{PUBLIC_URL}/{WEBHOOK_PATH}",
        "secret_token": TG_WEBHOOK_SECRET,
        "allowed_updates": json.dumps(["message","callback_query"]),
        "drop_pending_updates": True
    }
    r = requests.post(url, data=data, timeout=10)
    try:
        jr = r.json()
    except Exception:
        jr = {"text": r.text}
    log.info(f"setWebhook -> {jr}")

if __name__ == "__main__":
    # Автонастройка вебхука при старте
    try:
        ensure_webhook()
    except Exception as e:
        log.warning(f"setWebhook failed: {e}")

    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting web server on :{port}")
    app.run(host="0.0.0.0", port=port)
