# main.py — Innertrade Kai Mentor Bot (Improved Dialogue Version)
# Версия: 2025-09-01-empathic-v1

# ... (импорты и начальная часть кода остаются без изменений до MENTOR_SYSTEM_PROMPT)

# ========= НАСТРОЙКИ ДИАЛОГА =========
MIN_DIALOGUE_BEFORE_ANALYSIS = 3  # Минимум реплик перед предложением разбора
PATIENCE_LEVELS = {
    "low": 2,    # После 2 реплик предлагаем разбор
    "medium": 4, # После 4 реплик предлагаем разбор  
    "high": 6    # После 6 реплик предлагаем разбор
}

# ========= ОБНОВЛЕННЫЙ СИСТЕМНЫЙ ПРОМПТ =========
MENTOR_SYSTEM_PROMPT = """Ты — Алекс, эмпатичный и мудрый наставник для трейдеров. Твоя главная цель — помочь разобраться в проблемах, но сначала установить глубокий контакт.

Твои принципы:
1. Всегда начинай с установления раппорта — минимум 3-4 реплики до предложения разбора
2. Проявляй искренний интерес к проблеме человека
3. Задавай открытые вопросы для понимания контекста
4. Никогда не переходи к решению, не поняв глубину проблемы
5. Предлагай разбор только когда действительно видишь готовность и потребность

Пример правильного диалога:
- Сначала выясни, какие именно правила нарушаются
- Узнай, как долго это продолжается  
- Пойми, насколько это систематично
- Предложи выбрать ОДНО правило для работы
- Убедись, что проблема на уровне поведения/навыков
- И только потом предлагай глубокий разбор

Избегай шаблонных фраз. Будь человечным и warm."""

# ========= ОБНОВЛЕННАЯ ЛОГИКА ДИАЛОГА =========
def should_offer_deep_analysis(user_history: List[Dict]) -> bool:
    """Определяем, когда предлагать глубокий разбор"""
    if len(user_history) < MIN_DIALOGUE_BEFORE_ANALYSIS:
        return False
    
    last_user_messages = [msg["content"] for msg in user_history if msg["role"] == "user"][-3:]
    text = " ".join(last_user_messages).lower()
    
    # Ключевые phrases, указывающие на готовность к разбору
    readiness_phrases = [
        "систематически", "давно", "постоянно", "не могу справиться",
        "не получается", "надоело", "устал", "замкнутый круг"
    ]
    
    return any(phrase in text for phrase in readiness_phrases)

def build_dialogue_context(user_history: List[Dict]) -> str:
    """Строим контекст диалога для GPT"""
    context = "Предыдущие реплики в диалоге:\n"
    for i, msg in enumerate(user_history[-6:]):
        role = "Пользователь" if msg["role"] == "user" else "Ты"
        context += f"{i+1}. {role}: {msg['content']}\n"
    return context

# ========= ОБНОВЛЕННЫЙ GPT ДЕЦИЗИОН МЕЙКЕР =========
def gpt_decide(uid: int, text_in: str, st: Dict[str, Any]) -> Dict[str, Any]:
    # ... (предыдущий код функции остается без изменений до system_prompt)
    
    # Обновленный system_prompt с акцентом на диалог
    system_prompt = f"""
    {MENTOR_SYSTEM_PROMPT}
    
    Контекст диалога:
    {build_dialogue_context(history)}
    
    Текущее сообщение пользователя: {text_in}
    
    Важные правила:
    1. Сначала установи раппорт (минимум 3-4 реплики)
    2. Пойми глубину проблемы перед предложением решений
    3. Предложи разбор ТОЛЬКО если видишь готовность
    4. Помоги выбрать ОДНО правило для работы
    5. Убедись, что проблема на уровне поведения/навыков
    
    Ответ в формате JSON: next_step, intent, response_text, store, is_structural
    """
    
    # ... (остальная часть функции без изменений)

# ========= ОБНОВЛЕННАЯ ОБРАБОТКА ТЕКСТА =========
def handle_text_message(uid: int, text: str, original_message=None):
    """Обработка текстовых сообщений (общая функция)"""
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text[:80])

    # Update history (user)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "user", "content": text})
    st["data"]["history"] = history

    # Greeting: style selection
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text.lower() in ("ты", "вы"):
            st["data"]["style"] = text.lower()
            save_state(uid, intent=INTENT_FREE, step=STEP_FREE_INTRO, data=st["data"])
            response = f"Принято ({text}). Расскажи, что сейчас происходит в твоей торговле? Какие вызовы или сложности заметил?"
            bot.send_message(uid, response, reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Пожалуйста, выбери «ты» или «вы».", reply_markup=style_kb())
        return

    # Structural flow
    if st["intent"] == INTENT_ERR:
        handle_structural_flow(uid, text, st)
        return

    # Free flow — GPT с улучшенной логикой
    patterns = detect_trading_patterns(text)
    suggest_analysis = should_offer_deep_analysis(history)
    
    decision = gpt_decide(uid, text, st)
    resp = decision.get("response_text", "Понял. Расскажи подробнее.")

    # Update history (assistant)
    history = st["data"].get("history", [])
    if len(history) >= HIST_LIMIT:
        history = history[-(HIST_LIMIT-1):]
    history.append({"role": "assistant", "content": resp})

    merged = st["data"].copy()
    store = decision.get("store", {})
    if isinstance(store, dict):
        merged.update(store)
    merged["history"] = history

    new_intent = decision.get("intent", st["intent"])
    new_step = decision.get("next_step", st["step"])

    save_state(uid, intent=new_intent, step=new_step, data=merged)
    
    # Отправляем ответ
    if original_message:
        bot.reply_to(original_message, resp, reply_markup=MAIN_MENU)
    else:
        bot.send_message(uid, resp, reply_markup=MAIN_MENU)
    
    # Проактивное предложение помощи только после достаточного диалога
    if suggest_analysis and new_intent != INTENT_ERR and len(history) >= MIN_DIALOGUE_BEFORE_ANALYSIS:
        # Проверяем, что мы уже обсудили проблему
        user_msgs = [msg["content"] for msg in history if msg["role"] == "user"]
        problem_discussed = any(len(msg) > 20 for msg in user_msgs[-3:])
        
        if problem_discussed:
            bot.send_message(
                uid, 
                "Похоже, мы определили ключевую проблему. Хочешь разобрать её системно и найти коренную причину?",
                reply_markup=types.InlineKeyboardMarkup().row(
                    types.InlineKeyboardButton("Да, давай разберём", callback_data="deep_analysis_yes"),
                    types.InlineKeyboardButton("Сначала ещё обсудим", callback_data="deep_analysis_no")
                )
            )

# ========= НОВАЯ ФУНКЦИЯ ДЛЯ УГЛУБЛЕННОГО ДИАЛОГА =========
def handle_deep_dialogue(uid: int, text: str) -> str:
    """Обработка углубленного диалога перед анализом"""
    st = load_state(uid)
    history = st["data"].get("history", [])
    
    # Анализируем историю диалога
    user_messages = [msg["content"] for msg in history if msg["role"] == "user"]
    last_few_messages = user_messages[-3:] if len(user_messages) >= 3 else user_messages
    
    # Определяем стадию диалога
    if len(last_few_messages) < 2:
        return "Расскажи подробнее о том, что происходит. Какие именно правила нарушаются чаще всего?"
    
    # Вторая реплика - уточняем детали
    elif len(last_few_messages) < 3:
        return "Понимаю. Как долго это продолжается? И насколько это систематично?"
    
    # Третья реплика - предлагаем фокусировку
    elif len(last_few_messages) < 4:
        return "Ясно. Давай выберем ОДНУ самую болезненную проблему для работы. Какое нарушение больше всего мешает?"
    
    # Четвертая реплика - готовим к анализу
    else:
        return "Хорошо. Чтобы работать эффективно, давай сфокусируемся на этом. Готов разобрать это глубже?"