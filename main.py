# main.py — Innertrade (secure webhook edition)
import os, logging, time
from collections import deque, defaultdict
from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ---------- CONFIG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")   # не используем в этом файле, но пусть будет проверка
DATABASE_URL      = os.getenv("DATABASE_URL")
PUBLIC_URL        = os.getenv("PUBLIC_URL")       # например: https://innertrade-bot.onrender.com
WEBHOOK_PATH      = os.getenv("WEBHOOK_PATH", "hook")  # любая случайная строка
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")     # сгенерируй и положи в Secrets

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ограничения периметра
MAX_BODY_BYTES = 1_000_000  # 1 MB
RATE_WINDOW_S  = 60         # окно в секундах
RATE_LIMIT     = 120        # запросов на IP в окно (с запасом под батчи Telegram)

# ---------- DB ----------
engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None

def save_state(user_id: int, intent: str, step: str | None = None, data: dict | None = None):
    if not engine:
        return
    try:
        with engine.begin() as conn:
            # если дальше включим RLS — эта строка уже готова
            conn.execute(text("SET app.user_id = :uid"), {"uid": str(user_id)})
            conn.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data, updated_at)
                VALUES (:uid, :intent, :step, COALESCE(:data, '{}'::jsonb), now())
                ON CONFLICT (user_id) DO UPDATE
                SET intent = EXCLUDED.intent,
                    step   = EXCLUDED.step,
                    data   = EXCLUDED.data,
                    updated_at = now()
            """), {"uid": user_id, "intent": intent, "step": step, "data": data})
    except SQLAlchemyError as e:
        # не логируем данные, только мета
        logging.error(f"DB save_state failed for {user_id}: {e.__class__.__name__}")

# ---------- TELEGRAM BOT ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник *Innertrade*.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_menu()
    )
    save_state(m.from_user.id, "idle")

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

# --- интенты
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    save_state(m.from_user.id, "error")
    bot.send_message(
        m.chat.id,
        "Давай разберём через *MERCEDES + TOTE*.\n\n"
        "*M* Мотивация?\n*E* Эмоции?\n*R* Результат?\n*C* Контекст?\n*E* Эффект?\n*D* Действия?\n*S* Стратегия?\n\n"
        "*T* Test — что пошло не так?\n*O* Operate — что сделал?\n*T* Test — результат?\n*E* Evolve — что изменишь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    save_state(m.from_user.id, "strategy")
    bot.send_message(
        m.chat.id,
        "Ок, собираем ТС по конструктору:\n"
        "1) Цели\n2) Стиль (дневной/свинг/позиционный)\n"
        "3) Рынки/инструменты\n4) Правила входа/выхода\n"
        "5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование (история/демо)",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    save_state(m.from_user.id, "passport")
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    save_state(m.from_user.id, "week_panel")
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    save_state(m.from_user.id, "panic")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) Сделай 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану сделки или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    save_state(m.from_user.id, "start_help")
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

@bot.message_handler(content_types=["text"])
def fallback(m):
    bot.send_message(
        m.chat.id,
        "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.",
        reply_markup=main_menu()
    )

# ---------- FLASK APP (WEBHOOK ONLY) ----------
app = Flask(__name__)

# простейший rate-limit по IP
_hits: dict[str, deque] = defaultdict(deque)
def _client_ip():
    # Render/прокси могут ставить X-Forwarded-For
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"

@app.before_request
def guard():
    if request.path.startswith(f"/webhook/{WEBHOOK_PATH}"):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
            abort(401)
        if request.content_length and request.content_length > MAX_BODY_BYTES:
            abort(413)
        # rate limit
        now = time.time()
        dq = _hits[_client_ip()]
        while dq and now - dq[0] > RATE_WINDOW_S:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            abort(429)
        dq.append(now)

@app.get("/")
def root():
    return "OK (webhook)", 200

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.post(f"/webhook/{WEBHOOK_PATH}")
def webhook():
    try:
        upd = request.get_json(force=True, silent=False)
    except Exception:
        abort(400)
    try:
        update = types.Update.de_json(upd)
        bot.process_new_updates([update])
    except Exception as e:
        logging.error(f"update fail: {e.__class__.__name__}")
        # 200 чтобы Telegram не ретрайл миллион раз
        return jsonify({"ok": False}), 200
    return jsonify({"ok": True}), 200

def setup_webhook():
    # Сброс и установка вебхука с секретом (drop_pending_updates=True на всякий)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_PATH}"
    ok = bot.set_webhook(url=url, secret_token=TG_WEBHOOK_SECRET, drop_pending_updates=True)
    logging.info(f"Webhook set to {url}: {ok}")

if __name__ == "__main__":
    setup_webhook()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
