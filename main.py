import asyncio
import logging
import sys
import aiohttp
import requests
import sqlite3
import json
from typing import Dict, List, Optional, Union, Tuple

from aiogram import Bot, Dispatcher, html, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from calendar import monthcalendar
from datetime import datetime, date
import os
from dotenv import load_dotenv

import ssl


# Загружаем переменные из .env файла (если он есть)
load_dotenv()

# Получаем токен из переменных окружения
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is not set")

class AuthStates(StatesGroup):
    waiting_login = State()
    waiting_password = State()

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot.db')
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS authorized_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username VARCHAR(255) NOT NULL,
                telegram_username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                last_login TIMESTAMP,
                last_logout TIMESTAMP,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS auth_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES authorized_users(id)
            )
        ''')

        # Обновляем структуру связанных таблиц
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                filename TEXT,
                content BLOB,
                description TEXT,
                created_at TIMESTAMP,
                status TEXT DEFAULT 'pending',
                total_users INTEGER,
                sent_count INTEGER DEFAULT 0,
                last_sent_at TIMESTAMP,
                next_batch_time TIMESTAMP,
                recipients_type TEXT,
                recipients_list TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_deliveries (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                user_id INTEGER,
                status TEXT,
                error_message TEXT,
                attempted_at TIMESTAMP,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (report_id) REFERENCES reports(report_id),
                FOREIGN KEY (user_id) REFERENCES authorized_users(id)
            )
        ''')
        self.conn.commit()

    def is_username_active(self, username: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM authorized_users WHERE username = ? AND is_active = TRUE', (username,))
        return cursor.fetchone() is not None

    def add_user(self, telegram_id: int, username: str, user_data: dict) -> int:
        cursor = self.conn.cursor()
        
        # Деактивируем предыдущую активную сессию для этого username
        cursor.execute('''
            UPDATE authorized_users 
            SET is_active = FALSE, last_logout = CURRENT_TIMESTAMP 
            WHERE username = ? AND is_active = TRUE
        ''', (username,))
        
        # Добавляем новую запись или обновляем существующую
        cursor.execute('''
            INSERT INTO authorized_users (
                telegram_id, username, telegram_username, 
                first_name, last_name, last_login, is_active
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, TRUE)
        ''', (
            telegram_id,
            username,
            user_data.get('username'),
            user_data.get('first_name'),
            user_data.get('last_name')
        ))
        
        user_id = cursor.lastrowid
        
        # Логируем вход
        cursor.execute('''
            INSERT INTO auth_history (user_id, action)
            VALUES (?, 'login')
        ''', (user_id,))
        
        self.conn.commit()
        return user_id

    def remove_user(self, telegram_id: int):
        cursor = self.conn.cursor()

        cursor.execute('SELECT id FROM authorized_users WHERE telegram_id = ? AND is_active = TRUE', (telegram_id,))
        row = cursor.fetchone()
        if not row:
            return
            
        user_id = row[0]

        cursor.execute('''
            UPDATE authorized_users 
            SET is_active = FALSE, last_logout = CURRENT_TIMESTAMP 
            WHERE id = ?
        ''', (user_id,))
        
        # Логируем выход
        cursor.execute('''
            INSERT INTO auth_history (user_id, action)
            VALUES (?, 'logout')
        ''', (user_id,))
        
        self.conn.commit()

    def is_user_authorized(self, telegram_id: int) -> bool:
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM authorized_users WHERE telegram_id = ? AND is_active = TRUE', (telegram_id,))
        return cursor.fetchone() is not None

    def get_user_id(self, telegram_id: int) -> Optional[int]:
        cursor = self.conn.cursor()
        cursor.execute('SELECT id FROM authorized_users WHERE telegram_id = ? AND is_active = TRUE', (telegram_id,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_reports_by_date_range(
        self,
        date_from: datetime,
        date_to: datetime,
        page: int = 1,
        per_page: int = 10
    ) -> Dict:
        cursor = self.conn.cursor()
        offset = (page - 1) * per_page

        cursor.execute('''
            SELECT COUNT(*) FROM reports 
            WHERE created_at BETWEEN ? AND ?
        ''', (date_from, date_to))
        total = cursor.fetchone()[0]
        
        # Get reports
        cursor.execute('''
            SELECT r.*, u.username as creator_username
            FROM reports r
            JOIN authorized_users u ON r.user_id = u.id
            WHERE r.created_at BETWEEN ? AND ?
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
        ''', (date_from, date_to, per_page, offset))
        
        reports = []
        for row in cursor.fetchall():
            reports.append({
                'report_id': row[0],
                'filename': row[1],
                'description': row[3],
                'created_at': row[4],
                'status': row[5],
                'creator': row[-1]
            })
        
        return {
            'total': total,
            'page': page,
            'per_page': per_page,
            'reports': reports
        }

    def get_report_recipients(self, report_id: str) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT u.username, rd.status, rd.attempted_at
            FROM report_deliveries rd
            JOIN authorized_users u ON rd.user_id = u.id
            WHERE rd.report_id = ?
        ''', (report_id,))
        return [
            {'username': row[0], 'status': row[1], 'attempted_at': row[2]}
            for row in cursor.fetchall()
        ]

    def close(self):
        self.conn.close()
        
db = Database()

async def mock_auth_api(login: str, password: str) -> Tuple[bool, str]:
    # if login == "admin1" and password == "12345":
    #     return True, "Success"
    # else:
    #     return False, "Invalid credentials"

    url = "https://dev-v2-api.openinfo.uz/api/v2/userprofile/jwt/create/custom/"
    #url = "http://100.67.2.103:8000//api/v2/userprofile/jwt/create/custom/"


    payload = json.dumps({
        "username": login,
        "password": password
    })
    headers = {
        'Content-Type': 'application/json'
    }

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=payload) as response:
                if response.status == 200:
                    data = await response.text()
                    return True, "Success"

                elif response.status == 401:
                    return False, "Invalid credentials"

                elif response.status == 400:
                    data = await response.json()
                    error_msg = data.get('detail', 'Validation error')
                    return False, error_msg

                else:
                    return False, f"Server error (status: {response.status})"

    except aiohttp.ClientConnectorError:
        return False, "Connection error. API server is not available"
    except asyncio.TimeoutError:
        return False, "Request timeout. Please try again"
    except Exception as e:
        logging.error(f"Auth API error: {str(e)}")
        return False, "Unexpected error during authentication"

dp = Dispatcher(storage=MemoryStorage())

def get_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    keyboard = []

    keyboard.append([
        InlineKeyboardButton(
            text=f"{month}/{year}",  # используем named parameter text
            callback_data="ignore"
        )
    ])

    # Days
    for week in monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(
                    text=" ",  # используем named parameter text
                    callback_data="ignore"
                ))
            else:
                row.append(InlineKeyboardButton(
                    text=str(day),  # используем named parameter text
                    callback_data=f"date_{year}_{month}_{day}"
                ))
        keyboard.append(row)

    # Navigation
    keyboard.append([
        InlineKeyboardButton(
            text="◀️",  # используем named parameter text
            callback_data=f"prev_{year}_{month}"
        ),
        InlineKeyboardButton(
            text="▶️",  # используем named parameter text
            callback_data=f"next_{year}_{month}"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    if db.is_user_authorized(message.from_user.id):
        await message.answer(
            f"Welcome back, {html.bold(message.from_user.full_name)}!\n"
            "Use /exit if you want to log out.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
        
    await state.set_state(AuthStates.waiting_login)
    await message.answer(
        f"Your User ID: {message.from_user.id}\n"
        "Please enter your login:",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Command("exit"))
async def exit_handler(message: Message, state: FSMContext) -> None:
    if not db.is_user_authorized(message.from_user.id):
        await message.answer(
            "You are not logged in.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
        
    db.remove_user(message.from_user.id)
    await state.clear()
    await state.set_state(AuthStates.waiting_login)
    await message.answer(
        "You have been logged out. Please enter your login to authenticate again:",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(StateFilter(AuthStates.waiting_login))
async def process_login(message: Message, state: FSMContext) -> None:
    if message.text.startswith('/'):
        return
        
    await state.update_data(login=message.text)
    await state.set_state(AuthStates.waiting_password)
    await message.answer("Please enter your password:")

@dp.message(StateFilter(AuthStates.waiting_password))
async def process_password(message: Message, state: FSMContext) -> None:
    try:
        if message.text.startswith('/'):
            await message.delete()
            return
            
        await message.delete()
        
        data = await state.get_data()
        login = data.get("login")
        password = message.text

        # Проверяем, не авторизован ли уже кто-то с этим логином
        if db.is_username_active(login):
            await state.clear()
            await state.set_state(AuthStates.waiting_login)
            await message.answer(
                "This account is already in use. Please try another account or wait until it's logged out.",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        auth_success, error_message = await mock_auth_api(login, password)
        
        if auth_success:
            # Собираем данные пользователя
            user_data = {
                'username': message.from_user.username,
                'first_name': message.from_user.first_name,
                'last_name': message.from_user.last_name
            }
            
            db.add_user(message.from_user.id, login, user_data)
            await state.clear()
            await message.answer(
                f"Welcome, {html.bold(message.from_user.full_name)}!",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await state.clear()
            await state.set_state(AuthStates.waiting_login)
            await message.answer(
                f"Authentication failed: {error_message}\n"
                "Please enter your login again:",
                reply_markup=ReplyKeyboardRemove()
            )
            
    except Exception as e:
        logging.error(f"Error in process_password: {str(e)}")
        await state.clear()
        await state.set_state(AuthStates.waiting_login)
        await message.answer(
            "An error occurred during authentication. Please try again later.",
            reply_markup=ReplyKeyboardRemove()
        )

@dp.message(Command("reports"))
async def show_reports_calendar(message: Message, state: FSMContext):
    if not db.is_user_authorized(message.from_user.id):
        await message.answer(
            "Please authorize first using /start command",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    current_date = datetime.now()
    await message.answer(
        "Select date to view reports:",
        reply_markup=get_calendar_keyboard(current_date.year, current_date.month)
    )

@dp.callback_query(lambda c: c.data.startswith(("date_", "prev_", "next_")))
async def process_calendar_callback(callback_query: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await callback_query.message.delete()
        await callback_query.message.answer(
            "Bot was restarted. Please use /start command to authenticate again."
        )
        return

    data = callback_query.data.split("_")
    action = data[0]
    
    if action == "date":
        year, month, day = map(int, data[1:])
        selected_date = date(year, month, day)
        
        # Get reports for selected date
        reports = db.get_reports_by_date_range(
            date_from=datetime.combine(selected_date, datetime.min.time()),
            date_to=datetime.combine(selected_date, datetime.max.time()),
            page=1,
            per_page=10
        )
        
        if not reports['reports']:
            await callback_query.answer("No reports for this date")
            return
            
        # Format report list
        report_text = "Reports for {selected_date}:\n\n"
        for report in reports['reports']:
            report_text += (
                f"📄 {report['filename']}\n"
                f"📝 {report['description']}\n"
                f"👤 Created by: {report['creator']}\n"
                f"🕒 {report['created_at']}\n\n"
            )
            
        await callback_query.message.edit_text(
            report_text,
            reply_markup=get_calendar_keyboard(year, month)
        )
    
    elif action in ("prev", "next"):
        year, month = map(int, data[1:])
        if action == "prev":
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        else:
            month += 1
            if month > 12:
                month = 1
                year += 1
                
        await callback_query.message.edit_reply_markup(
            reply_markup=get_calendar_keyboard(year, month)
        )

@dp.message()
async def echo_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    
    if current_state is None and not db.is_user_authorized(message.from_user.id):
        await message.answer(
            "Bot was restarted. Please use /start command to authenticate again.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
        
    if db.is_user_authorized(message.from_user.id):
        try:
            await message.answer(
                "This bot will send you notifications with reports.",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logging.error(f"Error in echo_handler: {str(e)}")
            await message.answer("An error occurred. Please try again.")
    else:
        await message.answer(
            "Please authorize first using /start command",
            reply_markup=ReplyKeyboardRemove()
        )

async def main() -> None:
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    finally:
        db.close()