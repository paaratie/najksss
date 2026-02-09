import asyncio
import os
import json
import zipfile
import tempfile
import shutil
from typing import Dict, List, Optional, Tuple
from pyrogram import Client, idle
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardMarkup
)
from pyrogram.errors import SessionPasswordNeeded, BadRequest
import aiofiles
import aiofiles.os
import sqlite3
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
API_ID = 34185709
API_HASH = "b5c8271134295cde21ac6373128c0530"
BOT_TOKEN = "8427718534:AAGEejZgg1SsaPSoT5J962bQw3g4KLUWmXY"

# База данных
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('sessions.db')
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_name TEXT,
                session_path TEXT,
                phone TEXT,
                validated INTEGER DEFAULT 0,
                has_2fa INTEGER DEFAULT 0,
                created_at TIMESTAMP
            )
        ''')
        self.conn.commit()

    def add_session(self, user_id, session_name, session_path, phone):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sessions (user_id, session_name, session_path, phone, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, session_name, session_path, phone, datetime.now()))
        self.conn.commit()
        return cursor.lastrowid

    def get_user_sessions(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM sessions WHERE user_id = ? AND validated = 1', (user_id,))
        return cursor.fetchall()

    def delete_session(self, session_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT session_path FROM sessions WHERE id = ?', (session_id,))
        session = cursor.fetchone()
        if session and session[0] and os.path.exists(session[0]):
            try:
                os.remove(session[0])
            except:
                pass
        cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        self.conn.commit()

db = Database()

class SessionManager:
    def __init__(self):
        self.user_states = {}
        self.user_sessions = {}
        self.temp_dirs = {}
    
    async def process_zip_archive(self, user_id: int, zip_path: str) -> Tuple[int, int]:
        """Обработка ZIP архива с сессиями"""
        success_count = 0
        total_count = 0
        
        # Создаем временную директорию для пользователя
        temp_dir = tempfile.mkdtemp(prefix=f"tg_sessions_{user_id}_")
        self.temp_dirs[user_id] = temp_dir
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Список файлов в архиве
                file_list = zip_ref.namelist()
                
                # Фильтруем только файлы сессий
                session_files = [f for f in file_list if any(f.endswith(ext) for ext in ['.session', '.json', '.txt'])]
                
                for session_file in session_files:
                    total_count += 1
                    try:
                        # Извлекаем файл
                        zip_ref.extract(session_file, temp_dir)
                        session_path = os.path.join(temp_dir, session_file)
                        
                        # Валидируем сессию
                        is_valid, phone, user_id_tg = await self.validate_session_file(session_path, session_file)
                        
                        if is_valid:
                            # Сохраняем в постоянное хранилище
                            perm_dir = f"sessions/user_{user_id}"
                            os.makedirs(perm_dir, exist_ok=True)
                            
                            new_filename = f"{phone}_{session_file}" if phone else f"session_{total_count}_{session_file}"
                            perm_path = os.path.join(perm_dir, new_filename)
                            
                            shutil.copy(session_path, perm_path)
                            
                            # Добавляем в БД
                            db.add_session(user_id, new_filename, perm_path, phone)
                            success_count += 1
                            
                        # Удаляем временный файл
                        os.remove(session_path)
                        
                    except Exception as e:
                        logger.error(f"Error processing {session_file}: {e}")
                        continue
                
                # Проверяем наличие папки tdata
                if any('tdata/' in f for f in file_list):
                    tdata_files = [f for f in file_list if f.startswith('tdata/')]
                    
                    # Извлекаем всю папку tdata
                    tdata_dir = os.path.join(temp_dir, 'tdata')
                    os.makedirs(tdata_dir, exist_ok=True)
                    
                    for tdata_file in tdata_files:
                        try:
                            zip_ref.extract(tdata_file, temp_dir)
                        except:
                            pass
                    
                    # Пытаемся обработать tdata как телеграм десктоп сессии
                    success_count += await self.process_tdata_folder(user_id, tdata_dir)
                    total_count += 1
        
        except Exception as e:
            logger.error(f"Error processing ZIP: {e}")
        
        return success_count, total_count
    
    async def process_tdata_folder(self, user_id: int, tdata_path: str) -> int:
        """Обработка папки tdata (Telegram Desktop sessions)"""
        try:
            # Конвертируем tdata в pyrogram сессию
            # Это требует дополнительных библиотек для парсинга tdata
            # Здесь упрощенная логика
            
            # Ищем файлы авторизации
            auth_files = []
            for root, dirs, files in os.walk(tdata_path):
                for file in files:
                    if file.endswith('.map') or file == 'key_datas':
                        auth_files.append(os.path.join(root, file))
            
            if auth_files:
                # Сохраняем всю папку tdata
                perm_dir = f"sessions/user_{user_id}/tdata_{int(datetime.now().timestamp())}"
                shutil.copytree(tdata_path, perm_dir)
                
                # Добавляем в БД
                db.add_session(user_id, f"tdata_session_{len(auth_files)}", perm_dir, "tdata_session")
                return 1
        
        except Exception as e:
            logger.error(f"Error processing tdata: {e}")
        
        return 0
    
    async def validate_session_file(self, session_path: str, filename: str) -> Tuple[bool, Optional[str], Optional[int]]:
        """Валидация файла сессии"""
        try:
            if filename.endswith('.session'):
                # Telethon session
                session_name = os.path.basename(session_path).replace('.session', '')
                
                # Читаем файл сессии
                async with aiofiles.open(session_path, 'rb') as f:
                    session_data = await f.read()
                
                # Пытаемся создать клиент
                async with Client(
                    session_name,
                    API_ID,
                    API_HASH,
                    session_string=session_data.decode() if len(session_data) < 1000 else None
                ) as client:
                    try:
                        me = await client.get_me()
                        return True, me.phone_number, me.id
                    except:
                        # Пробуем через session_string
                        try:
                            client.session_string = session_data.decode('utf-8')
                            await client.connect()
                            me = await client.get_me()
                            return True, me.phone_number, me.id
                        except:
                            return False, None, None
            
            elif filename.endswith('.json'):
                # Pyrogram session JSON
                async with aiofiles.open(session_path, 'r', encoding='utf-8') as f:
                    session_json = json.loads(await f.read())
                
                session_string = session_json.get('session_string')
                if session_string:
                    session_name = f"pyro_{hash(session_string) % 10000}"
                    async with Client(session_name, API_ID, API_HASH, session_string=session_string) as client:
                        me = await client.get_me()
                        return True, me.phone_number, me.id
            
            elif filename.endswith('.txt'):
                # Session string в текстовом файле
                async with aiofiles.open(session_path, 'r', encoding='utf-8') as f:
                    content = await f.read().strip()
                
                if len(content) > 100:  # Предполагаем, что это session string
                    session_name = f"string_{hash(content) % 10000}"
                    async with Client(session_name, API_ID, API_HASH, session_string=content) as client:
                        me = await client.get_me()
                        return True, me.phone_number, me.id
        
        except Exception as e:
            logger.error(f"Validation error for {filename}: {e}")
        
        return False, None, None
    
    async def cleanup_temp_files(self, user_id: int):
        """Очистка временных файлов пользователя"""
        if user_id in self.temp_dirs:
            temp_dir = self.temp_dirs[user_id]
            try:
                shutil.rmtree(temp_dir)
                del self.temp_dirs[user_id]
            except:
                pass

session_manager = SessionManager()
app = Client("session_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Обработчик ZIP файлов
@app.on_message()
async def handle_zip_file(client: Client, message: Message):
    user_id = message.from_user.id
    
    if message.document:
        file_name = message.document.file_name
        
        if file_name.endswith('.zip'):
            # Сохраняем состояние - ожидаем ZIP файл
            session_manager.user_states[user_id] = {
                'waiting_for': 'zip_processing',
                'zip_file_id': message.document.file_id
            }
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Обработать ZIP", callback_data="process_zip_confirm")],
                [InlineKeyboardButton("❌ Отменить", callback_data="cancel_zip")]
            ])
            
            await message.reply_text(
                f"📦 Найден ZIP архив: `{file_name}`\n\n"
                "В архиве будут искаться файлы сессий (.session, .json, .txt) и папки tdata.\n\n"
                "Обработать архив?",
                reply_markup=keyboard
            )
            return
    
    # Если не ZIP, проверяем другие типы сессий
    await handle_regular_files(client, message)

@app.on_callback_query()
async def handle_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if data == "process_zip_confirm":
        # Получаем информацию о файле
        state = session_manager.user_states.get(user_id, {})
        if 'zip_file_id' in state:
            try:
                # Скачиваем файл
                file_id = state['zip_file_id']
                message = callback_query.message
                
                await callback_query.message.edit_text("📥 Скачиваю ZIP архив...")
                
                # Скачиваем файл
                download_path = await client.download_media(file_id, file_name=f"temp_{user_id}.zip")
                
                await callback_query.message.edit_text("📦 Распаковываю архив и проверяю сессии...")
                
                # Обрабатываем архив
                success_count, total_count = await session_manager.process_zip_archive(user_id, download_path)
                
                # Очищаем временные файлы
                await session_manager.cleanup_temp_files(user_id)
                if os.path.exists(download_path):
                    os.remove(download_path)
                
                # Обновляем состояние
                session_manager.user_states[user_id] = {}
                
                await callback_query.message.edit_text(
                    f"✅ Обработка завершена!\n\n"
                    f"📊 Результаты:\n"
                    f"• Всего файлов в архиве: {total_count}\n"
                    f"• Успешно добавлено сессий: {success_count}\n"
                    f"• Невалидных/ошибок: {total_count - success_count}\n\n"
                    f"Теперь у вас {len(db.get_user_sessions(user_id))} активных сессий."
                )
                
            except Exception as e:
                await callback_query.message.edit_text(f"❌ Ошибка при обработке ZIP: {str(e)}")
    
    elif data == "cancel_zip":
        session_manager.user_states[user_id] = {}
        await callback_query.message.edit_text("❌ Обработка ZIP архива отменена.")
    
    elif data == "upload_sessions":
        # Показываем инструкцию по загрузке
        await callback_query.message.edit_text(
            "📤 **Загрузка сессий**\n\n"
            "Вы можете загрузить:\n"
            "1. **ZIP архив** с сессиями - просто отправьте .zip файл\n"
            "2. **Отдельные файлы** сессий (.session, .json, .txt)\n"
            "3. **Папку tdata** (упакованную в ZIP)\n\n"
            "**Поддерживаемые форматы:**\n"
            "• `.session` - Telethon сессии\n"
            "• `.json` - Pyrogram сессии\n"
            "• `.txt` - Session strings\n"
            "• `tdata/` - Telegram Desktop папка\n\n"
            "Просто отправьте файл(ы) в этот чат.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
            ])
        )

# Главная функция
async def main():
    await app.start()
    print("✅ Бот запущен!")
    
    # Создаем директории если нет
    os.makedirs("sessions", exist_ok=True)
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())