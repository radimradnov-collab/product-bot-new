"""
Telegram-бот "Продукт → Режим → Результат"
Версия: 1.0 (MVP)
Архитектура: Конечный автомат состояний (FSM)

ВАЖНО:
- Бот не лечит, не диагностирует, не даёт медицинских рекомендаций
- Хранит только данные об использовании и субъективных ощущениях
"""

import os
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# ==================== КОНФИГУРАЦИЯ ====================

# Токен бота (заменить на свой)
BOT_TOKEN = "8483793056:AAECVHsX4yMTP4xWFdPmm_r2z6I7EGXMLD0"  # Или используй переменную окружения

# Состояния системы (FSM)
STATES = {
    'S0_INIT': 'S0',
    'S1_CONFIRM_CONDITIONS': 'S1',
    'S2_CHECK_CONTRAINDICATIONS': 'S2',
    'S3_READY_FOR_SESSION': 'S3',
    'S4_SESSION_ACTIVE': 'S4',
    'S5_POST_SESSION': 'S5',
    'S6_FEEDBACK': 'S6',
    'S7_REGULAR_USE': 'S7',
    'S8_PAUSE': 'S8'
}

# Длительность сеанса (в секундах)
# Для теста: 10 секунд, для продакшена: 300 (5 минут)
SESSION_DURATION = 10

# Тексты сообщений (нейтральный тон, без медицинских формулировок)
MESSAGES = {
    'S0': "Система помогает выстроить регулярное использование физического продукта. Это не медицинское изделие.",
    'S1': "Продукт используется только сидя, под собственным весом и в одежде.",
    'S2_QUESTIONS': [
        "Есть ли болевые ощущения?",
        "Есть ли ухудшение самочувствия?",
        "Есть ли головокружение или тошнота?",
        "Есть ли повышенная температура?",
        "Есть ли противопоказания?"
    ],
    'S3': "Первый сеанс адаптационный. Его задача — понять реакцию тела.",
    'S4': "Сеанс начат. Бот не пишет до завершения таймера.",
    'S5': "Сеанс завершён. Сделайте паузу и прислушайтесь к ощущениям.",
    'S6': "Как вы себя чувствуете?",
    'S6_DISCOMFORT': "Ощущения были неприятными или усиливающимися?",
    'S7': "Ощущения зафиксированы. Регулярность важнее единичных сеансов.",
    'S8': "Использование приостановлено. Возврат возможен только при отсутствии дискомфорта.",

    'ERROR': "Произошла ошибка. Пожалуйста, начните с /start",
    'HELP': (
        "Доступные команды:\n"
        "/start - запуск системы\n"
        "/status - текущее состояние\n"
        "/pause - принудительная пауза\n"
        "/resume - попытка возврата\n"
        "/help - эта справка\n\n"
        "Бот сопровождает использование физического продукта."
    ),
    'ALREADY_STARTED': "Система уже запущена. Используйте /status для проверки состояния.",
    'NO_PAUSE': "Пауза не активна. Используйте /status для проверки состояния.",
    'READY_FOR_NEXT': "Готовы к следующему сеансу?"
}

# ==================== БАЗА ДАННЫХ ====================

class Database:
    """Класс для работы с базой данных SQLite"""

    def __init__(self, db_name: str = 'product_bot.db'):
        self.db_name = db_name
        self.conn = None
        self.cursor = None
        self.init_database()

    def init_database(self):
        """Инициализация базы данных и создание таблиц"""
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        """Создание необходимых таблиц"""

        # Таблица пользователей
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                current_state TEXT DEFAULT 'S0',
                session_count INTEGER DEFAULT 0,
                pause_flag INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблица фидбэков
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS feedback_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER,
                feedback_type TEXT,
                discomfort_detail TEXT,
                session_number INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            )
        ''')

        # Таблица сессий
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER,
                session_number INTEGER,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            )
        ''')

        # Таблица лога состояний
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS state_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER,
                state TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            )
        ''')

        self.conn.commit()

    def get_or_create_user(self, user_id: int) -> Dict[str, Any]:
        """Получить пользователя или создать нового"""
        self.cursor.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (user_id,)
        )
        user = self.cursor.fetchone()

        if not user:
            # Создаем нового пользователя
            self.cursor.execute(
                "INSERT INTO users (telegram_user_id) VALUES (?)",
                (user_id,)
            )
            self.conn.commit()

            # Логируем начальное состояние
            self.log_state(user_id, STATES['S0_INIT'])

            return {
                'telegram_user_id': user_id,
                'current_state': STATES['S0_INIT'],
                'session_count': 0,
                'pause_flag': 0
            }

        return {
            'telegram_user_id': user[0],
            'current_state': user[1],
            'session_count': user[2],
            'pause_flag': user[3]
        }

    def update_user_state(self, user_id: int, state: str):
        """Обновить состояние пользователя"""
        self.cursor.execute(
            "UPDATE users SET current_state = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_user_id = ?",
            (state, user_id)
        )
        self.conn.commit()
        self.log_state(user_id, state)

    def increment_session_count(self, user_id: int) -> int:
        """Увеличить счетчик сессий пользователя"""
        self.cursor.execute(
            "UPDATE users SET session_count = session_count + 1 WHERE telegram_user_id = ?",
            (user_id,)
        )
        self.conn.commit()

        self.cursor.execute(
            "SELECT session_count FROM users WHERE telegram_user_id = ?",
            (user_id,)
        )
        return self.cursor.fetchone()[0]

    def set_pause_flag(self, user_id: int, pause_value: int) -> int:
        """Установить флаг паузы"""
        self.cursor.execute(
            "UPDATE users SET pause_flag = ? WHERE telegram_user_id = ?",
            (pause_value, user_id)
        )
        self.conn.commit()
        return pause_value

    def add_feedback(self, user_id: int, feedback_type: str,
                    discomfort_detail: Optional[str] = None,
                    session_number: Optional[int] = None):
        """Добавить запись фидбэка"""
        self.cursor.execute(
            """INSERT INTO feedback_log 
               (telegram_user_id, feedback_type, discomfort_detail, session_number) 
               VALUES (?, ?, ?, ?)""",
            (user_id, feedback_type, discomfort_detail, session_number)
        )
        self.conn.commit()

    def add_session(self, user_id: int, session_number: int, duration: int):
        """Добавить запись о сессии"""
        self.cursor.execute(
            """INSERT INTO sessions 
               (telegram_user_id, session_number, duration_seconds) 
               VALUES (?, ?, ?)""",
            (user_id, session_number, duration)
        )
        self.conn.commit()

    def log_state(self, user_id: int, state: str):
        """Записать состояние в лог"""
        self.cursor.execute(
            "INSERT INTO state_log (telegram_user_id, state) VALUES (?, ?)",
            (user_id, state)
        )
        self.conn.commit()

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Получить статистику пользователя"""
        self.cursor.execute(
            "SELECT session_count, pause_flag FROM users WHERE telegram_user_id = ?",
            (user_id,)
        )
        stats = self.cursor.fetchone()

        self.cursor.execute(
            """SELECT feedback_type, COUNT(*) 
               FROM feedback_log 
               WHERE telegram_user_id = ? 
               GROUP BY feedback_type""",
            (user_id,)
        )
        feedback_dist = dict(self.cursor.fetchall())

        return {
            'session_count': stats[0] if stats else 0,
            'pause_flag': stats[1] if stats else 0,
            'feedback_distribution': feedback_dist
        }

    def get_analytics(self) -> Dict[str, Any]:
        """Получить аналитику по всей системе"""
        self.cursor.execute("SELECT COUNT(DISTINCT telegram_user_id) FROM users")
        total_users = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT AVG(session_count) FROM users WHERE session_count > 0")
        avg_sessions = self.cursor.fetchone()[0] or 0

        self.cursor.execute(
            """SELECT feedback_type, COUNT(*) 
               FROM feedback_log 
               GROUP BY feedback_type"""
        )
        feedback_dist = dict(self.cursor.fetchall())

        return {
            'total_users': total_users,
            'average_sessions': round(avg_sessions, 2),
            'feedback_distribution': feedback_dist
        }

    def close(self):
        """Закрыть соединение с базой данных"""
        if self.conn:
            self.conn.close()

# ==================== КЛАВИАТУРЫ ====================

def get_keyboard(state: str) -> ReplyKeyboardMarkup:
    """Получить клавиатуру для указанного состояния"""

    if state == STATES['S0_INIT']:
        keyboard = [[KeyboardButton("Начать")]]

    elif state == STATES['S1_CONFIRM_CONDITIONS']:
        keyboard = [
            [KeyboardButton("Подтверждаю")],
            [KeyboardButton("Не подтверждаю")]
        ]

    elif state == STATES['S2_CHECK_CONTRAINDICATIONS']:
        keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]

    elif state == STATES['S3_READY_FOR_SESSION']:
        keyboard = [[KeyboardButton("Начать сеанс")]]

    elif state == STATES['S5_POST_SESSION']:
        keyboard = [[KeyboardButton("Продолжить")]]

    elif state == STATES['S6_FEEDBACK']:
        keyboard = [
            [KeyboardButton("Комфортно"), KeyboardButton("Нейтрально")],
            [KeyboardButton("Дискомфорт")]
        ]

    elif state == "DISCOMFORT_DETAIL":
        keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]

    else:
        keyboard = []

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

class CommandHandlers:
    """Обработчики команд бота"""

    def __init__(self, db: Database):
        self.db = db

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик команды /start"""
        user = update.effective_user
        user_data = self.db.get_or_create_user(user.id)

        # Если пользователь уже не в начальном состоянии и не на паузе
        if user_data['current_state'] != STATES['S0_INIT'] and user_data['pause_flag'] == 0:
            await update.message.reply_text(MESSAGES['ALREADY_STARTED'])
            return ConversationHandler.END

        # Сброс флага паузы при старте
        if user_data['pause_flag'] == 1:
            self.db.set_pause_flag(user.id, 0)

        # Устанавливаем начальное состояние
        self.db.update_user_state(user.id, STATES['S0_INIT'])

        # Отправляем приветственное сообщение
        await update.message.reply_text(
            MESSAGES['S0'],
            reply_markup=get_keyboard(STATES['S0_INIT'])
        )

        return STATES['S0_INIT']

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /status"""
        user = update.effective_user
        user_data = self.db.get_or_create_user(user.id)

        # Маппинг состояний на читаемые названия
        state_names = {
            STATES['S0_INIT']: "🔄 Инициализация",
            STATES['S1_CONFIRM_CONDITIONS']: "✅ Подтверждение условий",
            STATES['S2_CHECK_CONTRAINDICATIONS']: "⚠️ Проверка противопоказаний",
            STATES['S3_READY_FOR_SESSION']: "🎯 Готов к сеансу",
            STATES['S4_SESSION_ACTIVE']: "⏳ Сеанс активен",
            STATES['S5_POST_SESSION']: "📊 После сеанса",
            STATES['S6_FEEDBACK']: "💭 Обратная связь",
            STATES['S7_REGULAR_USE']: "📈 Регулярное использование",
            STATES['S8_PAUSE']: "⏸️ Пауза"
        }

        # Формируем текст статуса
        status_text = (
            f"📊 Ваш статус:\n\n"
            f"📍 Текущее состояние: {state_names.get(user_data['current_state'], 'Неизвестно')}\n"
            f"🔢 Количество сеансов: {user_data['session_count']}\n"
            f"⏸️ Режим паузы: {'Включен' if user_data['pause_flag'] == 1 else 'Выключен'}\n\n"
            f"ℹ️ Используйте /help для списка команд"
        )

        await update.message.reply_text(status_text)

    async def handle_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /pause"""
        user = update.effective_user

        # Устанавливаем флаг паузы
        self.db.set_pause_flag(user.id, 1)
        self.db.update_user_state(user.id, STATES['S8_PAUSE'])

        # Отменяем таймеры, если есть
        if 'session_timer' in context.user_data:
            if context.user_data['session_timer']:
                context.user_data['session_timer'].cancel()

        await update.message.reply_text(
            MESSAGES['S8'],
            reply_markup=ReplyKeyboardRemove()
        )

    async def handle_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /resume"""
        user = update.effective_user
        user_data = self.db.get_or_create_user(user.id)

        if user_data['pause_flag'] != 1:
            await update.message.reply_text(MESSAGES['NO_PAUSE'])
            return

        # Снимаем паузу и переходим к проверке противопоказаний
        self.db.set_pause_flag(user.id, 0)
        self.db.update_user_state(user.id, STATES['S2_CHECK_CONTRAINDICATIONS'])

        # Инициализируем индекс вопроса
        if 'current_question_index' not in context.user_data:
            context.user_data['current_question_index'] = {}
        context.user_data['current_question_index'][user.id] = 0

        # Задаем первый вопрос
        await update.message.reply_text(
            MESSAGES['S2_QUESTIONS'][0],
            reply_markup=get_keyboard(STATES['S2_CHECK_CONTRAINDICATIONS'])
        )

        return STATES['S2_CHECK_CONTRAINDICATIONS']

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        await update.message.reply_text(MESSAGES['HELP'])

# ==================== ОБРАБОТЧИКИ СОСТОЯНИЙ ====================

class StateHandlers:
    """Обработчики состояний FSM"""

    def __init__(self, db: Database):
        self.db = db

    async def handle_s0_init(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S0 - Инициализация"""
        user = update.effective_user

        # Переход к подтверждению условий
        self.db.update_user_state(user.id, STATES['S1_CONFIRM_CONDITIONS'])

        await update.message.reply_text(
            MESSAGES['S1'],
            reply_markup=get_keyboard(STATES['S1_CONFIRM_CONDITIONS'])
        )

        return STATES['S1_CONFIRM_CONDITIONS']

    async def handle_s1_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S1 - Подтверждение условий"""
        user = update.effective_user
        text = update.message.text

        if text == "Подтверждаю":
            # Переход к проверке противопоказаний
            self.db.update_user_state(user.id, STATES['S2_CHECK_CONTRAINDICATIONS'])

            # Инициализация индекса вопроса
            if 'current_question_index' not in context.user_data:
                context.user_data['current_question_index'] = {}
            context.user_data['current_question_index'][user.id] = 0

            # Первый вопрос проверки
            await update.message.reply_text(
                MESSAGES['S2_QUESTIONS'][0],
                reply_markup=get_keyboard(STATES['S2_CHECK_CONTRAINDICATIONS'])
            )

            return STATES['S2_CHECK_CONTRAINDICATIONS']
        else:
            # Переход в режим паузы
            self.db.update_user_state(user.id, STATES['S8_PAUSE'])
            self.db.set_pause_flag(user.id, 1)

            await update.message.reply_text(
                MESSAGES['S8'],
                reply_markup=ReplyKeyboardRemove()
            )

            return STATES['S8_PAUSE']

    async def handle_s2_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S2 - Проверка противопоказаний"""
        user = update.effective_user
        text = update.message.text

        # Если ответ "Да" на любой вопрос - переход в паузу
        if text == "Да":
            self.db.update_user_state(user.id, STATES['S8_PAUSE'])
            self.db.set_pause_flag(user.id, 1)

            await update.message.reply_text(
                MESSAGES['S8'],
                reply_markup=ReplyKeyboardRemove()
            )

            return STATES['S8_PAUSE']

        # Получаем текущий индекс вопроса
        if 'current_question_index' not in context.user_data:
            context.user_data['current_question_index'] = {}

        idx = context.user_data['current_question_index'].get(user.id, 0)

        # Переходим к следующему вопросу
        idx += 1

        # Если вопросы закончились
        if idx >= len(MESSAGES['S2_QUESTIONS']):
            # Переход к готовности сеанса
            self.db.update_user_state(user.id, STATES['S3_READY_FOR_SESSION'])

            await update.message.reply_text(
                MESSAGES['S3'],
                reply_markup=get_keyboard(STATES['S3_READY_FOR_SESSION'])
            )

            return STATES['S3_READY_FOR_SESSION']

        # Сохраняем индекс и задаем следующий вопрос
        context.user_data['current_question_index'][user.id] = idx

        await update.message.reply_text(
            MESSAGES['S2_QUESTIONS'][idx],
            reply_markup=get_keyboard(STATES['S2_CHECK_CONTRAINDICATIONS'])
        )

        return STATES['S2_CHECK_CONTRAINDICATIONS']

    async def handle_s3_ready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S3 - Готов к сеансу"""
        user = update.effective_user

        # Переход к активному сеансу
        self.db.update_user_state(user.id, STATES['S4_SESSION_ACTIVE'])

        # Отправляем сообщение о начале сеанса
        await update.message.reply_text(
            MESSAGES['S4'],
            reply_markup=ReplyKeyboardRemove()
        )

        # Запускаем таймер сеанса в фоновом режиме
        asyncio.create_task(self._session_timer(user.id, context))

        return STATES['S4_SESSION_ACTIVE']

    async def _session_timer(self, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Таймер сеанса (работает в фоне)"""
        try:
            # Ждем указанное время
            await asyncio.sleep(SESSION_DURATION)

            # Проверяем, не ушел ли пользователь в паузу
            user_data = self.db.get_or_create_user(user_id)
            if user_data['pause_flag'] == 1:
                return

            # Регистрируем завершение сеанса
            session_number = self.db.increment_session_count(user_id)
            self.db.add_session(user_id, session_number, SESSION_DURATION)

            # Переход к пост-сеансовому состоянию
            self.db.update_user_state(user_id, STATES['S5_POST_SESSION'])

            # Отправляем сообщение о завершении сеанса
            await context.bot.send_message(
                chat_id=user_id,
                text=MESSAGES['S5'],
                reply_markup=get_keyboard(STATES['S5_POST_SESSION'])
            )

        except Exception as e:
            logging.error(f"Ошибка в таймере сеанса: {e}")

    async def handle_s5_post_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S5 - После сеанса"""
        user = update.effective_user

        # Переход к сбору фидбэка
        self.db.update_user_state(user.id, STATES['S6_FEEDBACK'])

        await update.message.reply_text(
            MESSAGES['S6'],
            reply_markup=get_keyboard(STATES['S6_FEEDBACK'])
        )

        return STATES['S6_FEEDBACK']

    async def handle_s6_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик состояния S6 - Обратная связь"""
        user = update.effective_user
        text = update.message.text
        user_data = self.db.get_or_create_user(user.id)

        # Сохраняем фидбэк
        self.db.add_feedback(user.id, text, session_number=user_data['session_count'])

        if text == "Дискомфорт":
            # Нужны детали дискомфорта
            if 'discomfort_detail_needed' not in context.user_data:
                context.user_data['discomfort_detail_needed'] = {}
            context.user_data['discomfort_detail_needed'][user.id] = True

            await update.message.reply_text(
                MESSAGES['S6_DISCOMFORT'],
                reply_markup=get_keyboard("DISCOMFORT_DETAIL")
            )

            return STATES['S6_FEEDBACK']
        else:
            # Переход к завершению потока
            return await self._complete_feedback_flow(user.id, update, context)

    async def handle_s6_discomfort_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Обработчик деталей дискомфорта"""
        user = update.effective_user
        text = update.message.text
        user_data = self.db.get_or_create_user(user.id)

        if text == "Да":
            # Дискомфорт с усилением - переход в паузу
            self.db.add_feedback(
                user.id,
                "Дискомфорт с усилением",
                "Усиливающиеся ощущения",
                user_data['session_count']
            )

            self.db.update_user_state(user.id, STATES['S8_PAUSE'])
            self.db.set_pause_flag(user.id, 1)

            await update.message.reply_text(
                MESSAGES['S8'],
                reply_markup=ReplyKeyboardRemove()
            )

            return STATES['S8_PAUSE']
        else:
            # Дискомфорт без усиления - завершение потока
            self.db.add_feedback(
                user.id,
                "Дискомфорт без усиления",
                "Без усиления",
                user_data['session_count']
            )

            return await self._complete_feedback_flow(user.id, update, context)

    async def _complete_feedback_flow(self, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Завершение потока фидбэка"""
        # Переход к регулярному использованию
        self.db.update_user_state(user_id, STATES['S7_REGULAR_USE'])

        await update.message.reply_text(
            MESSAGES['S7'],
            reply_markup=ReplyKeyboardRemove()
        )

        # Ждем и предлагаем новый сеанс
        await asyncio.sleep(2)

        user_data = self.db.get_or_create_user(user_id)
        if user_data['pause_flag'] == 0:
            await context.bot.send_message(
                chat_id=user_id,
                text=MESSAGES['READY_FOR_NEXT'],
                reply_markup=get_keyboard(STATES['S3_READY_FOR_SESSION'])
            )

            self.db.update_user_state(user_id, STATES['S3_READY_FOR_SESSION'])
            return STATES['S3_READY_FOR_SESSION']

        return STATES['S7_REGULAR_USE']

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class ProductModeResultBot:
    """Главный класс Telegram-бота"""

    def __init__(self, token: str):
        self.token = token
        self.db = Database()
        self.command_handlers = CommandHandlers(self.db)
        self.state_handlers = StateHandlers(self.db)

        # Настройка логирования
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        self.logger = logging.getLogger(__name__)

    def create_application(self) -> Application:
        """Создание и настройка приложения бота"""

        # Создаем приложение
        application = Application.builder().token(self.token).build()

        # Добавляем обработчик ошибок
        application.add_error_handler(self._error_handler)

        # Создаем Conversation Handler для управления состояниями
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', self.command_handlers.handle_start)],
            states={
                STATES['S0_INIT']: [
                    MessageHandler(filters.Regex('^Начать$'), self.state_handlers.handle_s0_init)
                ],
                STATES['S1_CONFIRM_CONDITIONS']: [
                    MessageHandler(
                        filters.Regex('^(Подтверждаю|Не подтверждаю)$'),
                        self.state_handlers.handle_s1_confirm
                    )
                ],
                STATES['S2_CHECK_CONTRAINDICATIONS']: [
                    MessageHandler(
                        filters.Regex('^(Да|Нет)$'),
                        self.state_handlers.handle_s2_check
                    )
                ],
                STATES['S3_READY_FOR_SESSION']: [
                    MessageHandler(
                        filters.Regex('^Начать сеанс$'),
                        self.state_handlers.handle_s3_ready
                    )
                ],
                STATES['S4_SESSION_ACTIVE']: [
                    # В этом состоянии бот не обрабатывает сообщения
                ],
                STATES['S5_POST_SESSION']: [
                    MessageHandler(
                        filters.Regex('^Продолжить$'),
                        self.state_handlers.handle_s5_post_session
                    )
                ],
                STATES['S6_FEEDBACK']: [
                    MessageHandler(
                        filters.Regex('^(Комфортно|Нейтрально)$'),
                        self.state_handlers.handle_s6_feedback
                    ),
                    MessageHandler(
                        filters.Regex('^Дискомфорт$'),
                        self.state_handlers.handle_s6_feedback
                    ),
                    MessageHandler(
                        filters.Regex('^(Да|Нет)$'),
                        self.state_handlers.handle_s6_discomfort_detail
                    )
                ],
                STATES['S7_REGULAR_USE']: [
                    # Можно добавить переход к новому сеансу
                ],
                STATES['S8_PAUSE']: [
                    # Обработка через команду /resume
                ]
            },
            fallbacks=[
                CommandHandler('start', self.command_handlers.handle_start),
                CommandHandler('status', self.command_handlers.handle_status),
                CommandHandler('pause', self.command_handlers.handle_pause),
                CommandHandler('resume', self.command_handlers.handle_resume),
                CommandHandler('help', self.command_handlers.handle_help),
            ],
            allow_reentry=True
        )

        # Регистрируем обработчики
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('status', self.command_handlers.handle_status))
        application.add_handler(CommandHandler('pause', self.command_handlers.handle_pause))
        application.add_handler(CommandHandler('resume', self.command_handlers.handle_resume))
        application.add_handler(CommandHandler('help', self.command_handlers.handle_help))

        return application

    async def _error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        self.logger.error(f"Ошибка: {context.error}", exc_info=True)

        try:
            # Пытаемся уведомить пользователя
            await update.message.reply_text(MESSAGES['ERROR'])
        except:
            pass

    def run(self):
        """Запуск бота"""
        print("=" * 50)
        print("🚀 Запуск Telegram-бота 'Продукт → Режим → Результат'")
        print("=" * 50)
        print(f"📊 База данных: {self.db.db_name}")
        print(f"⏱  Длительность сеанса: {SESSION_DURATION} сек.")
        print(f"🔄 Режим паузы: /pause, /resume")
        print(f"📈 Статус: /status")
        print(f"❓ Помощь: /help")
        print("=" * 50)
        print("⏸  Для остановки нажмите Ctrl+C")
        print("=" * 50)

        # Создаем и запускаем приложение
        application = self.create_application()
        application.run_polling()

    def get_analytics(self) -> Dict[str, Any]:
        """Получить аналитику системы"""
        return self.db.get_analytics()

    def close(self):
        """Корректное завершение работы"""
        self.db.close()
        self.logger.info("Бот завершил работу")

# ==================== ЗАПУСК ПРОГРАММЫ ====================

if __name__ == '__main__':
    # Получаем токен (можно из переменной окружения или из файла)
    token = BOT_TOKEN

    # Если токен не указан, пробуем получить из переменной окружения
    if token == "ВАШ_ТОКЕН_ОТ_BOTFATHER":
        token = os.getenv("BOT_TOKEN", "")

    if not token:
        print("❌ ОШИБКА: Токен бота не найден!")
        print("")
        print("Инструкция по получению токена:")
        print("1. Откройте Telegram и найдите @BotFather")
        print("2. Отправьте команду /newbot")
        print("3. Укажите имя бота (например: Product Mode Result Bot)")
        print("4. Укажите username (должен заканчиваться на 'bot')")
        print("5. Скопируйте токен и вставьте в переменную BOT_TOKEN")
        print("")
        print("Или создайте файл .env и добавьте:")
        print("BOT_TOKEN=ваш_токен_здесь")
        exit(1)

    # Создаем и запускаем бота
    bot = ProductModeResultBot(token)

    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n\n👋 Завершение работы бота...")
    except Exception as e:
        print(f"\n\n❌ Критическая ошибка: {e}")
    finally:
        bot.close()

# ==================== КОМАНДЫ ДЛЯ ТЕСТИРОВАНИЯ ====================
"""
Команды для тестирования в Telegram:

/start - начать работу с ботом
/status - узнать текущий статус
/pause - принудительно поставить на паузу
/resume - возобновить с проверки противопоказаний
/help - получить справку

Состояния системы:
S0 - Инициализация
S1 - Подтверждение условий использования
S2 - Проверка противопоказаний (5 вопросов)
S3 - Готов к сеансу
S4 - Сеанс активен (таймер)
S5 - После сеанса
S6 - Сбор обратной связи
S7 - Регулярное использование
S8 - Пауза
"""