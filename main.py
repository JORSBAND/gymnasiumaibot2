import os
import asyncio
import uuid
import json
import logging
import re
import hashlib
from datetime import datetime, time as dt_time
from urllib.parse import parse_qs
from typing import Any, Callable, Dict

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler
import requests
from bs4 import BeautifulSoup
import pytz

# Firebase Imports
from firebase_admin import credentials, firestore, initialize_app
from firebase_admin.exceptions import FirebaseError

# Web Server Imports
from aiohttp import web, WSMsgType
import aiohttp_cors

# Global Firebase and App config
__app_id = "default-app-id"

# Налаштування логування
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Вбудований завантажувач облікових даних Firebase ---
def _load_service_account_from_env_or_file() -> dict | None:
    """
    Надійний завантажувач облікових даних:
      1) Змінна середовища FIREBASE_CREDENTIALS з JSON текстом
      2) Змінна середовища FIREBASE_CREDENTIALS зі шляхом до файлу
    Повертає розпарсений словник JSON або None.
    """
    import json, os, logging, base64
    env_val = os.environ.get("FIREBASE_CREDENTIALS")
    if env_val:
        env_strip = env_val.strip()
        if env_strip.startswith("{"):
            try:
                data = json.loads(env_strip)
                if isinstance(data.get("private_key"), str):
                    data["private_key"] = data["private_key"].replace("\\n", "\n")
                return data
            except json.JSONDecodeError as e:
                logging.error(f"Змінна середовища FIREBASE_CREDENTIALS виглядає як JSON, але не може бути розпарсена: {e}")
        else:
            try:
                decoded = base64.b64decode(env_val)
                try:
                    return json.loads(decoded.decode("utf-8"))
                except Exception:
                    pass
            except Exception:
                pass

            try:
                if os.path.exists(env_val):
                    with open(env_val, "r", encoding="utf-8") as f:
                        return json.load(f)
                else:
                    logging.warning(f"Надано змінну середовища FIREBASE_CREDENTIALS, але файл за шляхом '{env_val}' не знайдено.")
            except Exception as e:
                logging.error(f"Не вдалося завантажити FIREBASE_CREDENTIALS зі шляху '{env_val}': {e}")
    return None

db = None
try:
    sa_dict = _load_service_account_from_env_or_file()
    if sa_dict:
        cred = credentials.Certificate(sa_dict)
        try:
            initialize_app(cred)
            db = firestore.client()
            logger.info("Firebase ініціалізовано успішно.")
        except Exception as e_init:
            logger.info(f"initialize_app() викликала помилку: {e_init} - продовжуємо, якщо вже ініціалізовано.")
            if firestore.client():
                db = firestore.client()
    else:
        logger.error("Облікові дані Firebase не знайдено. Перевірте змінну середовища 'FIREBASE_CREDENTIALS'.")
except Exception as e:
    logger.error(f"Не вдалося ініціалізувати Firebase: {e}")
    db = None

# --- Конфігурація додатка ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEYS_STR = os.environ.get("GEMINI_API_KEYS")
GEMINI_API_KEYS = [key.strip() for key in GEMINI_API_KEYS_STR.split(',') if key.strip()] if GEMINI_API_KEYS_STR else []
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
STABILITY_AI_API_KEY = os.environ.get("STABILITY_AI_API_KEY")
WEBHOOK_URL = f"https://gymnasiumaibot.onrender.com/{TELEGRAM_BOT_TOKEN}"
ADMIN_IDS = [
    838464083,
    6484405296,
    1374181841,
    5268287971,
]
GYMNASIUM_URL = "https://brodygymnasium.e-schools.info"
TARGET_CHANNEL_ID = -1002946740131

# --- Глобальні змінні для веб-сервера ---
active_websockets: Dict[str, web.WebSocketResponse] = {}
web_sessions: Dict[str, Dict] = {} 

# --- Утиліти для роботи з Firestore ---
def get_collection_ref(collection_name: str, private=False, user_id=None):
    if db:
        if private and user_id:
            return db.collection('artifacts').document(__app_id).collection('users').document(user_id).collection(collection_name)
        else:
            return db.collection('artifacts').document(__app_id).collection('public').document('data').collection(collection_name)
    return None

async def load_data(collection_name: str, doc_id: str | None = None, private=False, user_id=None) -> Any:
    if not db:
        logger.error("З'єднання з базою даних не ініціалізовано.")
        return {}
    try:
        collection_ref = get_collection_ref(collection_name, private, user_id)
        if collection_ref is None:
            return {}
        if doc_id:
            doc_ref = collection_ref.document(doc_id)
            doc = await asyncio.to_thread(doc_ref.get)
            return doc.to_dict() if doc.exists else {}
        else:
            docs = await asyncio.to_thread(collection_ref.stream)
            data = {}
            for doc in docs:
                data[doc.id] = doc.to_dict()
            return data
    except FirebaseError as e:
        logger.error(f"Помилка завантаження Firebase для '{collection_name}': {e}")
        return {}

async def save_data(collection_name: str, data: Any, doc_id: str | None = None, private=False, user_id=None) -> None:
    if not db:
        logger.error("З'єднання з базою даних не ініціалізовано.")
        return
    try:
        collection_ref = get_collection_ref(collection_name, private, user_id)
        if collection_ref is None:
            return
        if doc_id:
            doc_ref = collection_ref.document(doc_id)
            await asyncio.to_thread(doc_ref.set, data, merge=True)
        else:
            await asyncio.to_thread(collection_ref.add, data)
        logger.info(f"Дані успішно збережено в колекції '{collection_name}'.")
    except FirebaseError as e:
        logger.error(f"Помилка збереження Firebase для '{collection_name}': {e}")

# --- Web App Утиліти ---
def get_user_from_init_data(init_data_str: str) -> dict | None:
    try:
        params = parse_qs(init_data_str)
        if 'user' in params:
            return json.loads(params['user'][0])
    except Exception as e:
        logger.error(f"Помилка парсингу init_data: {e}")
    return None

async def get_user_context(request: web.Request) -> dict | None:
    data = await request.json()
    init_data = data.get('initData')
    session_token = data.get('sessionToken')
    
    user = None
    if init_data: user = get_user_from_init_data(init_data)
    elif session_token and session_token in web_sessions: user = web_sessions.get(session_token)
    
    return user

async def send_reply_to_user(ptb_app: Application, user_id: str | int, text: str):
    conversations = await load_data('conversations', str(user_id), private=True, user_id=str(user_id))
    if not conversations:
        conversations = {}
    if 'messages' not in conversations:
        conversations['messages'] = []
    conversations['messages'].append({"sender": "bot", "text": text, "timestamp": datetime.now().isoformat()})
    await save_data('conversations', conversations, 'main', private=True, user_id=str(user_id))

    user_id_str = str(user_id)
    if user_id_str in active_websockets:
        try:
            await active_websockets[user_id_str].send_json({'type': 'message', 'payload': {'text': text}})
            logger.info(f"Надіслано відповідь через WS користувачу {user_id_str}")
        except Exception as e:
            logger.warning(f"Відправка через WS не вдалася для {user_id_str}: {e}")
            
    if isinstance(user_id, int):
        try:
            await ptb_app.bot.send_message(chat_id=user_id, text=text)
            logger.info(f"Надіслано відповідь через Telegram користувачу {user_id}")
        except Exception as e:
             logger.error(f"Не вдалося надіслати в Telegram користувачу {user_id}: {e}")

# --- Web App Обробники ---
async def handle_telegram_webhook(request: web.Request) -> web.Response:
    application = request.app['ptb_app']
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response()
    except json.JSONDecodeError:
        logger.warning("Не вдалося розпарсити JSON з вебхука Telegram.")
        return web.Response(status=400)
    except Exception as e:
        logger.error(f"Помилка в обробнику вебхука: {e}")
        return web.Response(status=500)

async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    user_id = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get('type') == 'auth':
                        payload = data.get('payload', {})
                        init_data = payload.get('initData')
                        session_token = payload.get('sessionToken')
                        
                        user_info = None
                        if init_data:
                            user_info = get_user_from_init_data(init_data)
                        elif session_token and session_token in web_sessions:
                            user_info = web_sessions[session_token]

                        if user_info and 'id' in user_info:
                            user_id = str(user_info.get('id'))
                            active_websockets[user_id] = ws
                            await ws.send_json({'type': 'auth_ok', 'payload': {'userId': user_id}})
                            logger.info(f"WebSocket для користувача {user_id} аутентифіковано.")
                        else:
                            await ws.send_json({'type': 'error', 'payload': {'message': 'Authentication failed'}})
                            await ws.close()
                            return ws
                except Exception as e:
                    logger.error(f"Помилка обробки WS повідомлення: {e}")
    finally:
        if user_id and user_id in active_websockets:
            del active_websockets[user_id]
        logger.info(f"WebSocket для користувача {user_id} закрито.")
    return ws

async def handle_api_init(request: web.Request) -> web.Response:
    data = await request.json()
    init_data, session_token = data.get('initData'), data.get('sessionToken')
    
    user = None
    if init_data: user = get_user_from_init_data(init_data)
    elif session_token and session_token in web_sessions: user = web_sessions.get(session_token)
    
    if not user or 'id' not in user: return web.json_response({'authStatus': 'required'})

    user_id_str = str(user['id'])
    history_doc = await load_data('conversations', 'main', private=True, user_id=user_id_str)
    history = history_doc.get('messages', []) if history_doc else []
    
    response_data = {'user': user, 'isAdmin': user['id'] in ADMIN_IDS, 'history': history}
    if session_token: response_data['sessionToken'] = session_token
    return web.json_response(response_data)

async def handle_api_login(request: web.Request) -> web.Response:
    data = await request.json()
    name, user_class = data.get('name'), data.get('class')
    if not name or not user_class: return web.json_response({'error': 'Name and class required'}, status=400)
    
    session_token = uuid.uuid4().hex
    user_id = f"web-{uuid.uuid4().hex[:8]}"
    user_data = {'id': user_id, 'first_name': name, 'username': f"{name} ({user_class})"}
    web_sessions[session_token] = user_data
    
    user_info = {'name': name, 'class': user_class, 'created_at': datetime.now().isoformat()}
    await save_data('users', user_info, user_id)
    
    return web.json_response({'user': user_data, 'sessionToken': session_token})

async def handle_send_message_web(request: web.Request) -> web.Response:
    user = await get_user_context(request)
    data = await request.json()
    text = data.get('text')
    if not user or not text: return web.json_response({'error': 'Auth or text missing'}, status=400)
    
    user_id, user_name = str(user['id']), user.get('first_name', 'User')
    
    conversations = await load_data('conversations', 'main', private=True, user_id=user_id) or {'messages': []}
    conversations['messages'].append({"sender": "user", "text": text, "timestamp": datetime.now().isoformat()})
    await save_data('conversations', conversations, 'main', private=True, user_id=user_id)

    forward_text = (f"📩 **Нове звернення (з Web App)**\n\n"
                    f"**Від:** {user_name} (ID: {user_id})\n\n"
                    f"**Текст:**\n---\n{text}")
    
    keyboard = [
        [InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_id}")],
        [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    for admin_id in ADMIN_IDS:
        await request.app['ptb_app'].bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
    return web.json_response({'status': 'ok'})

# --- Web App Admin Обробники ---
async def admin_action_wrapper(request: web.Request, action: Callable):
    user = await get_user_context(request)
    if not user or user.get('id') not in ADMIN_IDS:
        return web.json_response({'error': 'Unauthorized'}, status=403)
    return await action(request)

async def get_stats_web(request: web.Request):
    users_doc_ref = get_collection_ref('users')
    if users_doc_ref is None:
        return web.json_response({'error': 'Database connection is not initialized.'}, status=500)
    users_docs = await asyncio.to_thread(users_doc_ref.stream)
    user_count = sum(1 for _ in users_docs)
    return web.json_response({'user_count': user_count})

async def get_kb_view_web(request: web.Request):
    kb = await load_data('knowledge_base', 'main')
    return web.json_response(kb)

async def broadcast_web(request: web.Request):
    data = await request.json()
    message = data.get('message')
    if not message: return web.json_response({'error': 'Message required'}, status=400)
    
    ptb_app = request.app['ptb_app']
    success, fail = await do_broadcast(ptb_app, text_content=message)
    return web.json_response({'success': success, 'fail': fail})
    
async def get_conversations_web(request: web.Request):
    conversations_ref = get_collection_ref('conversations')
    if conversations_ref is None:
        return web.json_response({'error': 'Database connection is not initialized.'}, status=500)
    
    docs = await asyncio.to_thread(conversations_ref.stream)
    
    conv_list = []
    for doc in docs:
        data = doc.to_dict()
        messages = data.get('messages', [])
        user_id = doc.id
        
        if messages:
            user_name = f"User {user_id}"
            if user_id.startswith('web-'):
                user_doc = await load_data('users', user_id)
                if user_doc:
                    user_name = user_doc.get('name', user_name)
            
            conv_list.append({
                "user_id": user_id,
                "user_name": user_name,
                "last_message": messages[-1]['text'],
                "timestamp": messages[-1]['timestamp']
            })
    return web.json_response({"conversations": conv_list})

async def suggest_reply_web(request: web.Request):
    data = await request.json()
    user_id = data.get('user_id')
    history_doc = await load_data('conversations', 'main', private=True, user_id=str(user_id))
    history = history_doc.get('messages', []) if history_doc else []
    if not history: return web.json_response({"error": "No history found"}, status=404)
    
    history_text = "\n".join([f"{msg['sender']}: {msg['text']}" for msg in history[-5:]])
    prompt = (
        "Ти — помічник адміністратора шкільного чату. Проаналізуй історію переписки та запропонуй ввічливу та корисну відповідь від імені адміністратора. "
        "Відповідь має бути короткою та по суті.\n\n"
        f"ІСТОРІЯ:\n{history_text}\n\n"
        "ЗАПРОПОНОВАНА ВІДПОВІДЬ:"
    )
    reply = await generate_text_with_fallback(prompt)
    if not reply: return web.json_response({"error": "AI generation failed"}, status=500)
    return web.json_response({"reply": reply})

async def improve_text_web(request: web.Request):
    data = await request.json()
    text = data.get('text')
    if not text: return web.json_response({"error": "Text is required"}, status=400)

    prompt = (
        "Перепиши цей текст, щоб він був більш цікавим, лаконічним та привабливим для оголошення в шкільному телеграм-каналі. "
        "Збережи головну суть, але зроби стиль більш жвавим.\n\n"
        f"ОРИГІНАЛЬНИЙ ТЕКСТ:\n{text}\n\n"
        "ПОКРАЩЕНИЙ ТЕКСТ:"
    )
    improved_text = await generate_text_with_fallback(prompt)
    if not improved_text: return web.json_response({"error": "AI generation failed"}, status=500)
    return web.json_response({"improved_text": improved_text})

# --- Генерація тексту ---
async def generate_text_with_fallback(prompt: str) -> str | None:
    for api_key in GEMINI_API_KEYS:
        try:
            logger.info(f"Спроба використати Gemini API ключ, що закінчується на ...{api_key[-4:]}")
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = await asyncio.to_thread(model.generate_content, prompt, request_options={'timeout': 45})
            if response.text:
                logger.info("Успішна відповідь від Gemini.")
                return response.text
        except Exception as e:
            logger.warning(f"Gemini ключ ...{api_key[-4:]} не спрацював: {e}")
            continue
    logger.warning("Усі ключі Gemini не спрацювали. Переходжу до Cloudflare AI.")
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN or "your_cf" in CLOUDFLARE_ACCOUNT_ID:
        logger.error("Не налаштовано дані для Cloudflare AI.")
        return None

    try:
        cf_url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-2-7b-chat-int8"
        headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
        payload = {"messages": [{"role": "user", "content": prompt}]}
        response = await asyncio.to_thread(requests.post, cf_url, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        result = response.json()
        cf_text = result.get("result", {}).get("response")
        if cf_text:
            logger.info("Успішна відповідь від Cloudflare AI.")
            return cf_text
        else:
            logger.error(f"Cloudflare AI повернув порожню відповідь: {result}")
            return None
    except Exception as e:
        logger.error(f"Резервний варіант Cloudflare AI також не спрацював: {e}")
        return None

# --- Стани для ConversationHandler ---
(SELECTING_CATEGORY, IN_CONVERSATION, WAITING_FOR_REPLY,
 WAITING_FOR_ANONYMOUS_MESSAGE, WAITING_FOR_ANONYMOUS_REPLY,
 WAITING_FOR_BROADCAST_MESSAGE, CONFIRMING_BROADCAST,
 WAITING_FOR_KB_KEY, WAITING_FOR_KB_VALUE, CONFIRMING_AI_REPLY,
 WAITING_FOR_NEWS_TEXT, CONFIRMING_NEWS_ACTION, WAITING_FOR_MEDIA,
 SELECTING_TEST_USER, WAITING_FOR_TEST_NAME, WAITING_FOR_TEST_ID,
 WAITING_FOR_TEST_MESSAGE, WAITING_FOR_ADMIN_CONTACT, WAITING_FOR_KB_EDIT_VALUE,
 WAITING_FOR_SCHEDULE_TEXT, WAITING_FOR_SCHEDULE_TIME, CONFIRMING_SCHEDULE_POST) = range(22)

# --- Сповіщення для адмінів ---
async def get_admin_name(admin_id: int) -> str:
    admin_contacts = await load_data('admin_contacts')
    return admin_contacts.get(str(admin_id), f"Адміністратор {admin_id}")

async def notify_other_admins(context: ContextTypes.DEFAULT_TYPE, replying_admin_id: int, original_message_text: str) -> None:
    admin_name = await get_admin_name(replying_admin_id)
    notification_text = f"ℹ️ **{admin_name}** відповів на звернення:\n\n> _{original_message_text[:300]}..._"
    for admin_id in ADMIN_IDS:
        if admin_id != replying_admin_id:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Не вдалося надіслати сповіщення адміну {admin_id}: {e}")

# --- Універсальний розсильник ---
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE | Application, text_content: str, photo: bytes | str | None = None, video: str | None = None) -> tuple[int, int]:
    full_text_content = f"{text_content}"
    users_doc_ref = get_collection_ref('users')
    if users_doc_ref is None:
        logger.error("З'єднання з базою даних не ініціалізовано.")
        return 0, 0
    
    users_docs = await asyncio.to_thread(users_doc_ref.stream)
    user_ids = [doc.id for doc in users_docs if str(doc.id).isdigit()]
    
    success, fail = 0, 0
    for user_id in user_ids:
        try:
            if photo:
                await context.bot.send_photo(user_id, photo=photo, caption=full_text_content, parse_mode='Markdown')
            elif video:
                await context.bot.send_video(user_id, video=video, caption=full_text_content, parse_mode='Markdown')
            else:
                if len(full_text_content) > 4096:
                    for i in range(0, len(full_text_content), 4096):
                        await context.bot.send_message(user_id, text=full_text_content[i:i + 4096])
                else:
                    await context.bot.send_message(user_id, text=full_text_content)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Не вдалося надіслати повідомлення користувачу {user_id}: {e}")
            fail += 1
    return success, fail

async def generate_image(prompt: str) -> bytes | None:
    api_url = "https://api.stability.ai/v2beta/stable-image/generate/core"
    headers = {
        "authorization": f"Bearer {STABILITY_AI_API_KEY}",
        "accept": "image/*"
    }
    data = {
        "prompt": f"Minimalistic, symbolic, modern vector illustration for a school news article. The theme is: '{prompt}'. No text on the image, clean style.",
        "output_format": "jpeg",
        "aspect_ratio": "1:1"
    }
    try:
        response = await asyncio.to_thread(
            requests.post, api_url, headers=headers, files={"none": ''}, data=data, timeout=30
        )
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        logger.error(f"Помилка генерації зображення через Stability AI: {e}")
        if e.response is not None:
            logger.error(f"Відповідь сервера: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при генерації зображення: {e}")
        return None

def get_all_text_from_website() -> str | None:
    try:
        base = GYMNASIUM_URL.rstrip('/')
        url = base + "/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.extract()

        text = soup.body.get_text(separator='\n', strip=True)
        cleaned_text = re.sub(r'\n\s*\n', '\n\n', text)

        logger.info(f"З сайту отримано {len(cleaned_text)} символів тексту.")
        return cleaned_text if cleaned_text else None
    except requests.RequestException as e:
        logger.error(f"Помилка отримання даних з сайту: {e}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при парсингу сайту: {e}")
        return None

def get_teachers_info() -> str | None:
    try:
        url = "https://brodygymnasium.e-schools.info/teachers"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        content_area = soup.find('div', class_='content-inner')
        if content_area:
            for element in content_area(["script", "style"]):
                element.extract()
            text = content_area.get_text(separator='\n', strip=True)
            cleaned_text = re.sub(r'\n\s*\n', '\n', text)
            logger.info(f"Зі сторінки вчителів отримано {len(cleaned_text)} символів.")
            return cleaned_text
        else:
            logger.warning("Не знайдено блок 'content-inner' на сторінці вчителів.")
            return None
    except requests.RequestException as e:
        logger.error(f"Помилка отримання даних про вчителів: {e}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при парсингу сторінки вчителів: {e}")
        return None

async def gather_all_context(query: str) -> str:
    teacher_keywords = ['вчител', 'викладач', 'директор', 'завуч']
    is_teacher_query = any(keyword in query.lower() for keyword in teacher_keywords)

    site_text_task = asyncio.to_thread(get_all_text_from_website)
    teachers_info_task = asyncio.to_thread(get_teachers_info) if is_teacher_query else asyncio.sleep(0, result=None)

    site_text, teachers_info = await asyncio.gather(site_text_task, teachers_info_task)

    kb = await load_data('knowledge_base', 'main') or {}
    relevant_kb = {}
    if isinstance(kb, dict):
        qwords = set(query.lower().split())
        relevant_kb = {k: v for k, v in kb.items() if qwords & set(str(k).lower().split())}

    context_parts = []
    if teachers_info:
        context_parts.append(f"**Контекст зі сторінки вчителів:**\n{teachers_info[:2000]}")

    if site_text:
        context_parts.append(f"**Контекст з головної сторінки сайту:**\n{site_text[:2000]}")
    else:
        context_parts.append("**Контекст з сайту:**\nНе вдалося отримати.")

    if relevant_kb:
        context_parts.append(f"**Контекст з бази даних:**\n{json.dumps(relevant_kb, ensure_ascii=False)}")
    else:
        context_parts.append("**Контекст з бази даних:**\nНічого релевантного не знайдено.")

    return "\n\n".join(context_parts)

async def check_website_for_updates(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Виконую щоденну перевірку оновлень на сайті...")
    new_text = get_all_text_from_website()
    if not new_text:
        logger.info("Не вдалося отримати текст з сайту.")
        return

    last_check_data = await load_data('website_content', 'latest') or {}
    previous_text = last_check_data.get('text', '')

    if new_text != previous_text:
        logger.info("Знайдено оновлення на сайті!")
        await save_data('website_content', {'text': new_text, 'timestamp': datetime.now().isoformat()}, 'latest')
        await propose_website_update(context, new_text)

async def propose_website_update(context: ContextTypes.DEFAULT_TYPE, text_content: str):
    truncated_text = text_content[:800] + "..." if len(text_content) > 800 else text_content
    broadcast_id = f"website_update_{uuid.uuid4().hex[:8]}"
    
    context.bot_data.setdefault('scheduled_actions', {})[broadcast_id] = text_content

    keyboard = [
        [InlineKeyboardButton("Зробити розсилку 📢", callback_data=f"broadcast_website:{broadcast_id}")],
        [InlineKeyboardButton("Скасувати ❌", callback_data=f"cancel_website_update:{broadcast_id}")]
    ]
    message = f"**Знайдено оновлення на сайті!**\n\n**Новий вміст:**\n---\n{truncated_text}"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Не вдалося надіслати оновлення сайту адміну {admin_id}: {e}")

async def website_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, broadcast_id = query.data.split(':', 1)

    full_text = context.bot_data.get('scheduled_actions', {}).get(broadcast_id)
    if not full_text:
        await query.edit_message_text("Помилка: текст для розсилки застарів або не знайдено.")
        return
    
    if action == 'broadcast_website':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"📢 *Починаю розсилку оновлення з сайту...*")
        success, fail = await do_broadcast(context, text_content=full_text)
        await query.message.reply_text(f"✅ Розсилку оновлення завершено.\nНадіслано: {success}\nПомилок: {fail}")
    elif action == 'cancel_website_update':
        original_text = query.message.text
        new_text = f"{original_text}\n\n--- \n❌ **Скасовано.**"
        await query.edit_message_text(text=new_text, parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)

    if broadcast_id in context.bot_data.get('scheduled_actions', {}):
        del context.bot_data['scheduled_actions'][broadcast_id]

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("Створити новину ✍️", callback_data="admin_create_news"), InlineKeyboardButton("Запланувати новину 🗓️", callback_data="admin_schedule_news")],
        [InlineKeyboardButton("Заплановані пости 🕒", callback_data="admin_view_scheduled"), InlineKeyboardButton("Зробити розсилку 📢", callback_data="admin_broadcast")],
        [InlineKeyboardButton("Внести дані в базу ✍️", callback_data="admin_kb_add"), InlineKeyboardButton("Перевірити базу знань 🔎", callback_data="admin_kb_view")],
        [InlineKeyboardButton("Створити пост з сайту 📰", callback_data="admin_generate_post"), InlineKeyboardButton("Статистика 📊", callback_data="admin_stats")]
    ]
    await update.message.reply_text("🔐 **Адміністративна панель:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info_text_1 = ("🔐 **Інструкція для Адміністратора**\n\n"
        "Ось повний перелік функцій та команд, доступних для вас:\n\n"
        "--- \n"
        "**Основні Команди**\n\n"
        "• `/admin` - Відкриває головну адміністративну панель.\n"
        "• `/info` - Показує цю інструкцію.\n"
        "• `/faq` - Показує список поширених запитань з бази знань.\n"
        "• `/testm` - Запускає процес створення тестового звернення для перевірки.")
    info_text_2 = ("--- \n"
        "**Взаємодія зі Зверненнями**\n\n"
        "Коли користувач надсилає повідомлення, ви отримуєте сповіщення з кнопками:\n\n"
        "• **Відповісти особисто ✍️**: Натисніть, щоб бот попросив вас ввести відповідь.\n"
        "• **Відповісти за допомогою ШІ 🤖**: Бот генерує відповідь на основі даних з сайту та бази знань. Вам буде показано попередній перегляд.\n"
        "• **Пряма відповідь (Reply)**: Використовуйте функцію \"Reply\" в Telegram на повідомленні від бота, і ваша відповідь буде автоматично перенаправлена користувачу.\n\n"
        "Коли один адмін відповідає, інші отримують сповіщення.")
    info_text_3 = ("--- \n"
        "**Функції Адмін-панелі (`/admin`)**\n\n"
        "• **Статистика 📊**: Кількість унікальних користувачів бота.\n"
        "• **Створити новину ✍️**: Створює пост для негайної розсилки.\n"
        "• **Запланувати новину 🗓️**: Створює пост для розсилки у вказаний час.\n"
        "• **Заплановані пости 🕒**: Показує список запланованих постів з можливістю їх скасування.\n"
        "• **Зробити розсилку 📢**: Швидкий спосіб надіслати текстове повідомлення всім користувачам.\n"
        "• **Внести дані в базу ✍️**: Додає нову інформацію (питання-відповідь) до бази знань.\n"
        "• **Перевірити базу знань 🔎**: Показує весь вміст бази з кнопками для редагування/видалення.\n"
        "• **Створити пост з сайту 📰**: Автоматично генерує новину з головної сторінки сайту.")
    info_text_4 = ("--- \n"
        "**Тестові Команди**\n\n"
        "• `/testsite` - Перевіряє доступ до сайту гімназії.\n"
        "• `/testai` - Перевіряє роботу ШІ.\n"
        "• `/testimage` - Перевіряє генерацію зображень.\n\n"
        "--- \n"
        "**Важливо:**\n"
        "• Адміністратори не можуть створювати звернення через загальний функціонал, щоб уникнути плутанини. Використовуйте `/testm` для тестування.")
    await update.message.reply_text(info_text_1, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_2, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_3, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_4, parse_mode='Markdown')

async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()
    
    users_doc_ref = get_collection_ref('users')
    if users_doc_ref is None:
        await query.edit_message_text("❌ Помилка: Не вдалося підключитися до бази даних.")
        return
    
    users_docs = await asyncio.to_thread(users_doc_ref.stream)
    user_count = sum(1 for _ in users_docs)
    await query.edit_message_text(f"📊 **Статистика бота:**\n\nВсього унікальних користувачів: {user_count}", parse_mode='Markdown')

async def start_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Введіть **ключ** для нових даних (наприклад, 'Директор').\n\nДля скасування введіть /cancel.", parse_mode='Markdown')
    return WAITING_FOR_KB_KEY

async def get_kb_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['kb_key'] = update.message.text
    await update.message.reply_text(f"Ключ '{update.message.text}' збережено. Тепер введіть **значення**.", parse_mode='Markdown')
    return WAITING_FOR_KB_VALUE

async def get_kb_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.chat_data.pop('kb_key', None)
    value = update.message.text
    if not key:
        await update.message.reply_text("Ключ не знайдено. Повторіть операцію.", parse_mode='Markdown')
        return ConversationHandler.END
        
    kb_doc = await load_data('knowledge_base', 'main') or {}
    if not isinstance(kb_doc, dict): kb_doc = {}
    kb_doc[key] = value
    await save_data('knowledge_base', kb_doc, 'main')
    
    await update.message.reply_text(f"✅ Дані успішно збережено!\n\n**{key}**: {value}", parse_mode='Markdown')
    return ConversationHandler.END

async def view_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    kb = await load_data('knowledge_base', 'main') or {}
    if not kb or not isinstance(kb, dict):
        await query.edit_message_text("База знань порожня або пошкоджена.")
        return

    await query.edit_message_text("Ось вміст бази знань. Ви можете редагувати або видаляти записи.")
    
    context.bot_data.setdefault('kb_key_map', {}).clear()
    for key, value in kb.items():
        key_hash = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        context.bot_data['kb_key_map'][key_hash] = key

        keyboard = [[InlineKeyboardButton("Редагувати ✏️", callback_data=f"kb_edit:{key_hash}"), InlineKeyboardButton("Видалити 🗑️", callback_data=f"kb_delete:{key_hash}")]]
        text = f"**Ключ:** `{key}`\n\n**Значення:**\n`{value}`"
        
        if len(text) > 4000: text = text[:4000] + "..."
            
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        await asyncio.sleep(0.1)

async def delete_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    key_hash = query.data.split(':', 1)[1]
    key_to_delete = context.bot_data.get('kb_key_map', {}).get(key_hash)

    if not key_to_delete:
        await query.edit_message_text(f"❌ Помилка: цей запит застарів. Будь ласка, відкрийте базу знань знову.", parse_mode='Markdown')
        return

    kb = await load_data('knowledge_base', 'main') or {}
    if key_to_delete in kb:
        del kb[key_to_delete]
        await save_data('knowledge_base', kb, 'main')
        await query.edit_message_text(f"✅ Запис з ключем `{key_to_delete}` видалено.", parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ Помилка: запис з ключем `{key_to_delete}` не знайдено (можливо, вже видалено).", parse_mode='Markdown')

async def start_kb_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key_hash = query.data.split(':', 1)[1]
    key_to_edit = context.bot_data.get('kb_key_map', {}).get(key_hash)

    if not key_to_edit:
        await query.message.reply_text("❌ Помилка: цей запит застарів. Будь ласка, відкрийте базу знань знову і спробуйте ще раз.")
        return ConversationHandler.END

    context.chat_data['key_to_edit'] = key_to_edit
    
    kb = await load_data('knowledge_base', 'main') or {}
    current_value = kb.get(key_to_edit, "Не знайдено")

    await query.message.reply_text(
        f"Редагування запису.\n**Ключ:** `{key_to_edit}`\n"
        f"**Поточне значення:** `{current_value}`\n\n"
        "Введіть нове значення для цього ключа.\n\n/cancel для скасування.",
        parse_mode='Markdown'
    )
    return WAITING_FOR_KB_EDIT_VALUE

async def get_kb_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key_to_edit = context.chat_data.pop('key_to_edit', None)
    new_value = update.message.text
    if not key_to_edit:
        await update.message.reply_text("❌ Помилка: ключ для редагування втрачено. Спробуйте знову.")
        return ConversationHandler.END
    kb = await load_data('knowledge_base', 'main') or {}
    if not isinstance(kb, dict): kb = {}
    kb[key_to_edit] = new_value
    await save_data('knowledge_base', kb, 'main')
    await update.message.reply_text(f"✅ Запис успішно оновлено!\n\n**{key_to_edit}**: {new_value}", parse_mode='Markdown')
    return ConversationHandler.END

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = await load_data('knowledge_base', 'main') or {}
    if not kb or not isinstance(kb, dict):
        await update.message.reply_text("Наразі поширених запитань немає.")
        return
    context.bot_data.setdefault('faq_key_map', {}).clear()
    buttons = []
    for key in kb.keys():
        key_hash = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        context.bot_data['faq_key_map'][key_hash] = key
        buttons.append([InlineKeyboardButton(key, callback_data=f"faq_key:{key_hash}")])
    if not buttons:
        await update.message.reply_text("Наразі поширених запитань немає.")
        return
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Ось список поширених запитань:", reply_markup=reply_markup)

async def faq_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key_hash = query.data.split(':', 1)[1]
    key = context.bot_data.get('faq_key_map', {}).get(key_hash)
    if not key:
        await query.message.reply_text("Вибачте, це питання застаріло.")
        return
    kb = await load_data('knowledge_base', 'main') or {}
    answer = kb.get(key)
    if answer:
        await query.message.reply_text(f"**{key}**\n\n{answer}", parse_mode='Markdown')
    else:
        await query.message.reply_text("Відповідь на це питання не знайдено.")

async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    logger.info(f"Виконую заплановану розсилку: {job_data.get('text', '')[:30]}")
    await do_broadcast(context, text_content=job_data.get('text', ''), photo=job_data.get('photo'), video=job_data.get('video'))

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs: return False
    for job in current_jobs: job.schedule_removal()
    return True

async def start_schedule_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Надішліть текст для запланованої новини. /cancel для скасування.")
    return WAITING_FOR_SCHEDULE_TEXT

async def get_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['schedule_text'] = update.message.text
    context.chat_data['schedule_photo'] = None 
    context.chat_data['schedule_video'] = None
    
    await update.message.reply_text("Текст збережено. Тепер введіть дату та час для розсилки.\n\n"
        "**Формат: `ДД.ММ.РРРР ГГ:ХХ`**\n"
        "Наприклад: `25.12.2024 18:30`\n\n"
        "/cancel для скасування.", parse_mode='Markdown')
    return WAITING_FOR_SCHEDULE_TIME

async def get_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    kyiv_timezone = pytz.timezone("Europe/Kyiv")
    
    try:
        schedule_time = datetime.strptime(time_str, "%d.%m.%Y %H:%M")
        schedule_time_aware = kyiv_timezone.localize(schedule_time)
        
        if schedule_time_aware < datetime.now(kyiv_timezone):
            await update.message.reply_text("❌ Вказаний час вже минув. Будь ласка, введіть майбутню дату та час.")
            return WAITING_FOR_SCHEDULE_TIME
            
        context.chat_data['schedule_time_str'] = schedule_time_aware.strftime("%d.%m.%Y о %H:%M")
        context.chat_data['schedule_time_obj'] = schedule_time_aware

        text = context.chat_data['schedule_text']
        preview_message = (f"**Попередній перегляд запланованого поста:**\n\n"
            f"{text}\n\n"
            f"---\n"
            f"🗓️ Запланувати розсилку на **{context.chat_data['schedule_time_str']}**?")
        
        keyboard = [[InlineKeyboardButton("Так, запланувати ✅", callback_data="confirm_schedule_post")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data="cancel_schedule_post")]]
        
        await update.message.reply_text(preview_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIRMING_SCHEDULE_POST

    except ValueError:
        await update.message.reply_text("❌ **Неправильний формат дати.**\n"
            "Будь ласка, введіть дату та час у форматі `ДД.ММ.РРРР ГГ:ХХ`.\n"
            "Наприклад: `25.12.2024 18:30`")
        return WAITING_FOR_SCHEDULE_TIME

async def confirm_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    post_data = {
        'text': context.chat_data.get('schedule_text'),
        'photo': context.chat_data.get('schedule_photo'),
        'video': context.chat_data.get('schedule_video'),
    }
    schedule_time = context.chat_data.get('schedule_time_obj')
    
    if not post_data['text'] or not schedule_time:
        await query.edit_message_text("❌ Помилка: дані для планування втрачено. Почніть знову.")
        return ConversationHandler.END

    job_id = f"scheduled_post_{uuid.uuid4().hex[:10]}"
    context.job_queue.run_once(scheduled_broadcast_job, when=schedule_time, data=post_data, name=job_id)
    time_str = context.chat_data.get('schedule_time_str', 'невідомий час')
    await query.edit_message_text(f"✅ **Пост успішно заплановано на {time_str}.**", parse_mode='Markdown')
    context.chat_data.clear()
    return ConversationHandler.END

async def cancel_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Планування скасовано.")
    context.chat_data.clear()
    return ConversationHandler.END

async def view_scheduled_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    jobs = context.job_queue.jobs()
    scheduled_posts = [j for j in jobs if j.name and j.name.startswith("scheduled_post_")]
    
    if not scheduled_posts:
        await query.edit_message_text("Немає запланованих постів.")
        return

    await query.edit_message_text("**Список запланованих постів:**", parse_mode='Markdown')
    kyiv_timezone = pytz.timezone("Europe/Kyiv")

    for job in scheduled_posts:
        run_time = job.next_t.astimezone(kyiv_timezone).strftime("%d.%m.%Y о %H:%M")
        text = job.data.get('text', '')[:200]
        
        message = (f"🗓️ **Час відправки:** {run_time}\n\n"
            f"**Текст:**\n_{text}..._")
        
        keyboard = [[InlineKeyboardButton("Скасувати розсилку ❌", callback_data=f"cancel_job:{job.name}")]]
        await query.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        await asyncio.sleep(0.1)

async def cancel_scheduled_job_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    job_name = query.data.split(':', 1)[1]
    
    if remove_job_if_exists(job_name, context):
        await query.edit_message_text("✅ Заплановану розсилку скасовано.")
    else:
        await query.edit_message_text("❌ Цей пост вже було надіслано або скасовано раніше.")

async def generate_post_from_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ *Збираю дані з сайту...*", parse_mode='Markdown')

    site_text = await asyncio.to_thread(get_all_text_from_website)
    if not site_text:
        await query.edit_message_text("❌ Не вдалося отримати дані з сайту. Спробуйте пізніше.")
        return
    try:
        await query.edit_message_text("🧠 *Аналізую текст та створюю пост...*", parse_mode='Markdown')
        summary_prompt = ("Проаналізуй наступний текст з веб-сайту. Створи з нього короткий, цікавий та інформативний пост для телеграм-каналу. "
            "Виділи найголовнішу думку або новину. Пост має бути написаний українською мовою.\n\n"
            f"--- ТЕКСТ З САЙТУ ---\n{site_text[:2500]}\n\n"
            "--- ПОСТ ДЛЯ ТЕЛЕГРАМ-КАНАЛУ ---")
        post_text = await generate_text_with_fallback(summary_prompt)
        if not post_text:
            await query.edit_message_text("❌ Не вдалося згенерувати текст поста. Усі системи ШІ недоступні.")
            return

        await query.edit_message_text("🎨 *Генерую зображення...*", parse_mode='Markdown')
        image_prompt_for_ai = ("На основі цього тексту, створи короткий опис (3-7 слів) англійською мовою для генерації зображення. Опис має бути символічним та мінімалістичним.\n\n"
            f"Текст: {post_text[:300]}")
        image_prompt = await generate_text_with_fallback(image_prompt_for_ai)
        image_bytes = await generate_image(image_prompt.strip() if image_prompt else "school news")

        post_id = uuid.uuid4().hex[:8]
        context.bot_data[f"manual_post_{post_id}"] = {'text': post_text, 'photo': image_bytes}
        keyboard = [[InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]]
        caption = f"{post_text}\n\n---\n*Робити розсилку цієї новини?*"

        await query.delete_message()
        if image_bytes:
            await context.bot.send_photo(chat_id=query.from_user.id, photo=image_bytes, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text=f"{caption}\n\n(Не вдалося згенерувати зображення)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Помилка при створенні поста з сайту: {e}")
        try:
            await query.edit_message_text(f"❌ *Сталася помилка:* {e}")
        except:
            await context.bot.send_message(query.from_user.id, f"❌ *Сталася помилка:* {e}")

async def handle_post_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, post_id = query.data.split(':', 1)
    post_data_key = f"manual_post_{post_id}"
    post_data = context.bot_data.get(post_data_key)
    if not post_data:
        await query.edit_message_text("Помилка: цей пост застарів або вже був оброблений.")
        return
    if action == 'confirm_post':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("📢 *Починаю розсилку поста...*")
        success, fail = await do_broadcast(context, text_content=post_data['text'], photo=post_data.get('photo'), video=post_data.get('video'))
        await query.message.reply_text(f"✅ Розсилку завершено.\nНадіслано: {success}\nПомилок: {fail}")
    elif action == 'cancel_post':
        original_caption = query.message.caption or query.message.text
        text_to_keep = original_caption.split("\n\n---\n")[0]
        if query.message.photo:
            await query.edit_message_caption(caption=f"{text_to_keep}\n\n--- \n❌ **Скасовано.**", parse_mode='Markdown')
        else:
            await query.edit_message_text(text=f"{text_to_keep}\n\n--- \n❌ **Скасовано.**", parse_mode='Markdown')
    if post_data_key in context.bot_data: del context.bot_data[post_data_key]

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post = update.channel_post
    if not post or post.chat.id != TARGET_CHANNEL_ID: return
    post_text = post.text or post.caption or ""
    if not post_text: return
    logger.info(f"Отримано пост з цільового каналу: {post_text[:50]}...")
    channel_posts = await load_data('channel_posts', 'main') or {}
    posts = channel_posts.get('posts', [])
    posts.insert(0, post_text)
    posts = posts[:20]
    await save_data('channel_posts', {'posts': posts}, 'main')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in ADMIN_IDS:
        await admin_command_entry(update, context, command_handler=admin_panel)
        return
    user_id = str(update.effective_user.id)
    user_data = await load_data('users', user_id)
    if not user_data:
        user_info = {
            'id': update.effective_user.id,
            'first_name': update.effective_user.first_name,
            'username': update.effective_user.username,
            'created_at': datetime.now().isoformat()
        }
        await save_data('users', user_info, user_id)
    await update.message.reply_text('Вітаємо! Це офіційний бот каналу новин Бродівської гімназії.\n\n'
        '➡️ Напишіть ваше запитання або пропозицію, щоб відправити її адміністратору.\n'
        '➡️ Використовуйте команду /anonymous, щоб надіслати анонімне звернення.\n'
        '➡️ Використовуйте /faq для перегляду поширених запитань.')

async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("Адміністратори не можуть створювати звернення. Використовуйте /admin для доступу до панелі.")
        return ConversationHandler.END
    message = update.message
    user_data = context.user_data
    user_id = str(update.effective_user.id)
    text = message.text or message.caption or ""
    conversations_doc = await load_data('conversations', 'main', private=True, user_id=user_id) or {'messages': []}
    conversations_doc['messages'].append({"sender": "user", "text": text, "timestamp": datetime.now().isoformat()})
    await save_data('conversations', conversations_doc, 'main', private=True, user_id=user_id)
    user_data['user_info'] = {'id': user_id, 'name': update.effective_user.full_name}

    if message.text:
        user_data['user_message'] = message.text
        user_data['media_type'] = None
        user_data['file_id'] = None
    elif message.photo:
        user_data['user_message'] = message.caption or ""
        user_data['media_type'] = 'photo'
        user_data['file_id'] = message.photo[-1].file_id
    elif message.video:
        user_data['user_message'] = message.caption or ""
        user_data['media_type'] = 'video'
        user_data['file_id'] = message.video.file_id
    else: return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("Запитання ❓", callback_data="category_question")], [InlineKeyboardButton("Пропозиція 💡", callback_data="category_suggestion")], [InlineKeyboardButton("Скарга 📄", callback_data="category_complaint")]]
    await update.message.reply_text("Будь ласка, оберіть категорію вашого звернення:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_CATEGORY

async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    category_map = {"category_question": "Запитання ❓", "category_suggestion": "Пропозиція 💡", "category_complaint": "Скарга 📄"}
    category = category_map.get(query.data, "Без категорії")
    user_data = context.user_data
    user_data['category'] = category
    user_message = user_data.get('user_message', '')
    user_info = user_data.get('user_info', {'id': update.effective_user.id, 'name': update.effective_user.full_name})
    media_type = user_data.get('media_type')
    file_id = user_data.get('file_id')
    keyboard = [[InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")], [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    forward_text = (f"📩 **Нове звернення**\n\n"
                    f"**Категорія:** {category}\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{user_message}")
    for admin_id in ADMIN_IDS:
        try:
            if media_type == 'photo': await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif media_type == 'video': await context.bot.send_video(chat_id=admin_id, video=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не змогли переслати звернення адміну {admin_id}: {e}")
    await query.edit_message_text("✅ Дякуємо! Ваше повідомлення надіслано. Якщо у вас є доповнення, просто напишіть їх наступним повідомленням.")
    return IN_CONVERSATION

async def continue_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_info = context.user_data.get('user_info', {'id': update.effective_user.id, 'name': update.effective_user.full_name})
    category = context.user_data.get('category', 'Без категорії')
    user_id = str(update.effective_user.id)
    text = update.message.text or update.message.caption or ""
    conversations_doc = await load_data('conversations', 'main', private=True, user_id=user_id) or {'messages': []}
    conversations_doc['messages'].append({"sender": "user", "text": text, "timestamp": datetime.now().isoformat()})
    await save_data('conversations', conversations_doc, 'main', private=True, user_id=user_id)
    keyboard = [[InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")], [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    forward_text = (f"➡️ **Доповнення до розмови**\n\n"
                    f"**Категорія:** {category}\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{text}")
    for admin_id in ADMIN_IDS:
        try:
            if update.message.photo: await context.bot.send_photo(admin_id, photo=update.message.photo[-1].file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif update.message.video: await context.bot.send_video(admin_id, video=update.message.video.file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=admin_id, text=forward_text, parse_mode='Markdown', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Не змогли переслати доповнення адміну {admin_id}: {e}")
    await update.message.reply_text("✅ Доповнення надіслано.")
    return IN_CONVERSATION

async def anonymous_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Напишіть ваше анонімне повідомлення... Для скасування введіть /cancel.")
    return WAITING_FOR_ANONYMOUS_MESSAGE

async def receive_anonymous_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    anon_id = str(uuid.uuid4())[:8]
    user_id = str(update.effective_user.id)
    anonymous_map = await load_data('anonymous_map', 'main') or {}
    anonymous_map[anon_id] = user_id
    await save_data('anonymous_map', anonymous_map, 'main')
    message_text = update.message.text
    conversations_doc = await load_data('conversations', 'main', private=True, user_id=user_id) or {'messages': []}
    conversations_doc['messages'].append({"sender": "user", "text": f"(Анонімно) {message_text}", "timestamp": datetime.now().isoformat()})
    await save_data('conversations', conversations_doc, 'main', private=True, user_id=user_id)
    keyboard = [[InlineKeyboardButton("Відповісти з ШІ 🤖", callback_data=f"anon_ai_reply:{anon_id}")], [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"anon_reply:{anon_id}")] ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    forward_text = f"🤫 **Нове анонімне звернення (ID: {anon_id})**\n\n**Текст:**\n---\n{message_text}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не вдалося переслати анонімне адміну {admin_id}: {e}")
    await update.message.reply_text("✅ Ваше анонімне повідомлення надіслано.")
    return ConversationHandler.END

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Напишіть повідомлення для розсилки. /cancel для скасування.")
        return WAITING_FOR_BROADCAST_MESSAGE
    return ConversationHandler.END

async def get_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['broadcast_message'] = update.message.text
    users_doc_ref = get_collection_ref('users')
    if users_doc_ref is None:
        await update.message.reply_text("❌ Помилка: Не вдалося підключитися до бази даних.")
        return ConversationHandler.END
    users_docs = await asyncio.to_thread(users_doc_ref.stream)
    user_count = sum(1 for _ in users_docs)
    keyboard = [[InlineKeyboardButton("Так, надіслати ✅", callback_data="confirm_broadcast")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data="cancel_broadcast")]]
    await update.message.reply_text(f"**Попередній перегляд:**\n\n{update.message.text}\n\n---\nНадіслати **{user_count}** користувачам?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CONFIRMING_BROADCAST

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("📢 *Починаю розсилку...*", parse_mode='Markdown')
    message_text = context.chat_data.get('broadcast_message', '')
    success, fail = await do_broadcast(context, text_content=message_text)
    await query.edit_message_text(f"✅ Розсилку завершено.\nНадіслано: {success}\nПомилок: {fail}")
    context.chat_data.clear()
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Розсилку скасовано.")
    context.chat_data.clear()
    return ConversationHandler.END

async def start_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    action, target_user_id_str = query.data.split(':', 1)
    context.chat_data['target_user_id'] = target_user_id_str
    original_text = query.message.text or query.message.caption or ""
    user_question_part = original_text.split('---\n')
    context.chat_data['original_user_message'] = user_question_part[-1] if user_question_part else ""

    if action == "manual_reply":
        await query.edit_message_text(text=f"{original_text}\n\n✍️ *Напишіть вашу відповідь. /cancel для скасування*", parse_mode='Markdown')
        return WAITING_FOR_REPLY
    elif action == "ai_reply":
        await query.edit_message_text(text=f"{original_text}\n\n🤔 *Генерую відповідь (це може зайняти до 45 секунд)...*", parse_mode='Markdown')
        try:
            user_question = context.chat_data.get('original_user_message', '')
            if not user_question: raise ValueError("Не вдалося отримати текст запиту користувача.")
            logger.info("Збираю контекст для відповіді ШІ...")
            additional_context = await gather_all_context(user_question)
            prompt = ("Ти — корисний асистент для адміністратора шкільного телеграм-каналу. Дай відповідь на запитання користувача. "
                "Спочатку проаналізуй наданий контекст. Якщо він релевантний, використай його для відповіді. Якщо ні, відповідай на основі загальних знань.\n\n"
                f"--- КОНТЕКСТ (з сайту та бази знань) ---\n{additional_context}\n\n"
                f"--- ЗАПИТАННЯ КОРИСТУВАЧА ---\n'{user_question}'\n\n"
                f"--- ВІДПОВІДЬ ---\n")
            ai_response_text = await generate_text_with_fallback(prompt)
            if not ai_response_text: raise ValueError("Не вдалося згенерувати відповідь. Усі системи ШІ недоступні.")
            context.chat_data['ai_response'] = ai_response_text
            keyboard = [[InlineKeyboardButton("Надіслати відповідь ✅", callback_data=f"send_ai_reply:{context.chat_data['target_user_id']}")], [InlineKeyboardButton("Скасувати ❌", callback_data="cancel_ai_reply")]]
            preview_text = f"🤖 **Ось відповідь від ШІ:**\n\n{ai_response_text}\n\n---\n*Надіслати цю відповідь користувачу?*"
            await query.edit_message_text(text=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            return CONFIRMING_AI_REPLY
        except Exception as e:
            logger.error(f"Помилка генерації відповіді ШІ: {e}")
            await query.edit_message_text(text=f"{original_text}\n\n❌ *Помилка генерації відповіді ШІ: {e}*", parse_mode='Markdown')
            return ConversationHandler.END

async def send_ai_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    ai_response_text = context.chat_data.get('ai_response')
    target_user_id = context.chat_data.get('target_user_id')
    original_message = context.chat_data.get('original_user_message', 'Невідоме звернення')
    
    if not ai_response_text or not target_user_id:
        await query.edit_message_text("❌ Помилка: дані для відповіді втрачено. Спробуйте знову.")
        return ConversationHandler.END
    try:
        target_user_id_typed = int(target_user_id) if str(target_user_id).isdigit() else target_user_id
        await send_reply_to_user(context.application, target_user_id_typed, ai_response_text)
        await query.edit_message_text(text="✅ *Відповідь успішно надіслано.*", parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)
        await notify_other_admins(context, query.from_user.id, original_message)
    except Exception as e:
        logger.error(f"Помилка надсилання відповіді ШІ користувачу {target_user_id}: {e}")
        await query.edit_message_text(text=f"❌ *Помилка надсилання відповіді: {e}*", parse_mode='Markdown')
    context.chat_data.clear()
    return ConversationHandler.END

async def receive_manual_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_user_id = context.chat_data.get('target_user_id')
    original_message = context.chat_data.get('original_user_message', 'Невідоме звернення')
    if not target_user_id:
        await update.message.reply_text("❌ Не знайдено цільового користувача.")
        return ConversationHandler.END
    owner_reply_text = update.message.text
    try:
        target_user_id_typed = int(target_user_id) if str(target_user_id).isdigit() else target_user_id
        await send_reply_to_user(context.application, target_user_id_typed, f"✉️ **Відповідь від адміністратора:**\n\n{owner_reply_text}")
        await update.message.reply_text("✅ Вашу відповідь надіслано.")
        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}")
    context.chat_data.clear()
    return ConversationHandler.END

async def start_anonymous_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)
    context.chat_data['anon_id_to_reply'] = anon_id
    original_text = query.message.text or ""
    user_question = original_text.split('---\n')[-1].strip()
    context.chat_data['original_user_message'] = user_question
    await query.edit_message_text(text=f"{original_text}\n\n🤔 *Генерую відповідь для аноніма (це може зайняти до 45 секунд)...*", parse_mode='Markdown')
    try:
        if not user_question: raise ValueError("Не вдалося отримати текст анонімного запиту.")
        logger.info("Збираю контекст для відповіді ШІ аноніму...")
        additional_context = await gather_all_context(user_question)
        prompt = ("Ти — корисний асистент. Дай відповідь на анонімне запитання. Будь ввічливим та інформативним.\n\n"
            f"--- КОНТЕКСТ (з сайту та бази знань) ---\n{additional_context}\n\n"
            f"--- АНОНІМНЕ ЗАПИТАННЯ ---\n'{user_question}'\n\n"
            f"--- ВІДПОВІДЬ ---\n")
        ai_response_text = await generate_text_with_fallback(prompt)
        if not ai_response_text: raise ValueError("Не вдалося згенерувати відповідь. Усі системи ШІ недоступні.")
        context.chat_data['ai_response'] = ai_response_text
        keyboard = [[InlineKeyboardButton("Надіслати відповідь ✅", callback_data=f"send_anon_ai_reply:{anon_id}")], [InlineKeyboardButton("Скасувати ❌", callback_data="cancel_ai_reply")]]
        preview_text = f"🤖 **Ось відповідь від ШІ для аноніма (ID: {anon_id}):**\n\n{ai_response_text}\n\n---\n*Надіслати цю відповідь?*"
        await query.edit_message_text(text=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIRMING_AI_REPLY
    except Exception as e:
        logger.error(f"Помилка генерації відповіді ШІ для аноніма: {e}")
        await query.edit_message_text(text=f"{original_text}\n\n❌ *Помилка генерації відповіді ШІ: {e}*", parse_mode='Markdown')
        return ConversationHandler.END

async def send_anonymous_ai_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)
    ai_response_text = context.chat_data.get('ai_response')
    anonymous_map = await load_data('anonymous_map', 'main') or {}
    user_id = anonymous_map.get(anon_id)
    original_message = context.chat_data.get('original_user_message', 'Невідоме анонімне звернення')
    if not ai_response_text or not user_id:
        await query.edit_message_text("❌ Помилка: дані для відповіді аноніму втрачено.")
        return ConversationHandler.END
    try:
        await send_reply_to_user(context.application, int(user_id), f"🤫 **Відповідь на ваше анонімне звернення (від ШІ):**\n\n{ai_response_text}")
        await query.edit_message_text(text="✅ *Відповідь аноніму успішно надіслано.*", parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)
        await notify_other_admins(context, query.from_user.id, original_message)
    except Exception as e:
        logger.error(f"Помилка надсилання ШІ-відповіді аноніму {user_id}: {e}")
        await query.edit_message_text(text=f"❌ *Помилка надсилання відповіді: {e}*", parse_mode='Markdown')
    context.chat_data.clear()
    return ConversationHandler.END

async def start_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)
    context.chat_data['anon_id_to_reply'] = anon_id
    original_text = query.message.text or ""
    user_question = original_text.split('---\n')[-1].strip()
    context.chat_data['original_user_message'] = user_question
    await query.message.reply_text(f"✍️ Напишіть вашу відповідь для аноніма (ID: {anon_id}). /cancel для скасування.")
    return WAITING_FOR_ANONYMOUS_REPLY

async def send_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    anon_id = context.chat_data.get('anon_id_to_reply')
    anonymous_map = await load_data('anonymous_map', 'main') or {}
    user_id = anonymous_map.get(anon_id)
    original_message = context.chat_data.get('original_user_message', 'Невідоме анонімне звернення')
    
    if not user_id:
        await update.message.reply_text("❌ Помилка: не знайдено отримувача.")
        return ConversationHandler.END
    admin_reply_text = update.message.text
    try:
        await send_reply_to_user(context.application, int(user_id), f"🤫 **Відповідь на ваше анонімне звернення:**\n\n{admin_reply_text}")
        await update.message.reply_text(f"✅ Вашу відповідь аноніму (ID: {anon_id}) надіслано.")
        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}")
    context.chat_data.clear()
    return ConversationHandler.END

async def handle_admin_direct_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    replied_message = update.message.reply_to_message
    if not replied_message or replied_message.from_user.id != context.bot.id: return
    target_user_id = None
    text_to_scan = replied_message.text or replied_message.caption or ""
    original_message = text_to_scan.split('---\n')[-1].strip()
    
    match = re.search(r"\(ID: ([\w\-]+)\)", text_to_scan)
    if match:
        target_user_id_str = match.group(1)
        try: target_user_id = int(target_user_id_str)
        except ValueError: target_user_id = target_user_id_str
        reply_intro = "✉️ **Відповідь від адміністратора:**"
    else:
        anon_match = re.search(r"\(ID: ([a-f0-9\-]+)\)", text_to_scan)
        if anon_match:
            anon_id = anon_match.group(1)
            anonymous_map = await load_data('anonymous_map', 'main') or {}
            user_id_from_map = anonymous_map.get(anon_id)
            if user_id_from_map:
                try: target_user_id = int(user_id_from_map)
                except ValueError: pass
            reply_intro = "🤫 **Відповідь на ваше анонімне звернення:**"
    if not target_user_id: return
    try:
        reply_text = update.message.text or update.message.caption or ""
        if update.message.photo or update.message.video:
            if isinstance(target_user_id, str) and target_user_id.startswith('web-'):
                 await send_reply_to_user(context.application, target_user_id, f"{reply_intro}\n\n{reply_text}\n\n(Адміністратор також надіслав медіа, яке неможливо відобразити тут)")
            else:
                 if update.message.photo: await context.bot.send_photo(chat_id=target_user_id, photo=update.message.photo[-1].file_id, caption=f"{reply_intro}\n\n{reply_text}", parse_mode='Markdown')
                 elif update.message.video: await context.bot.send_video(chat_id=target_user_id, video=update.message.video.file_id, caption=f"{reply_intro}\n\n{reply_text}", parse_mode='Markdown')
        else:
            await send_reply_to_user(context.application, target_user_id, f"{reply_intro}\n\n{reply_text}")
        await update.message.reply_text("✅ Вашу відповідь надіслано.", quote=True)
        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        logger.error(f"Не вдалося надіслати пряму відповідь користувачу {target_user_id}: {e}")
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}", quote=True)

async def start_news_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Будь ласка, надішліть текст для вашої новини. /cancel для скасування.")
    return WAITING_FOR_NEWS_TEXT

async def get_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['news_text'] = update.message.text
    keyboard = [[InlineKeyboardButton("Обробити через ШІ 🤖", callback_data="news_ai")], [InlineKeyboardButton("Вручну додати медіа 🖼️", callback_data="news_manual")]]
    await update.message.reply_text("Текст збережено. Як продовжити?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRMING_NEWS_ACTION

async def handle_news_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data
    news_text = context.chat_data.get('news_text')
    if not news_text:
        await query.edit_message_text("❌ Помилка: текст новини втрачено. Почніть знову.")
        return ConversationHandler.END
    if action == 'news_ai':
        try:
            await query.edit_message_text("🧠 *Обробляю текст та створюю заголовок...*", parse_mode='Markdown')
            summary_prompt = f"Перепиши цей текст, щоб він був цікавим та лаконічним постом для телеграм-каналу новин. Збережи головну суть. Текст:\n\n{news_text}"
            processed_text = await generate_text_with_fallback(summary_prompt)
            if not processed_text:
                await query.edit_message_text("❌ Не вдалося обробити текст. Усі системи ШІ недоступні.")
                return ConversationHandler.END
            await query.edit_message_text("🎨 *Генерую зображення...*", parse_mode='Markdown')
            image_prompt_for_ai = ("На основі цього тексту, створи короткий опис (3-7 слів) англійською мовою для генерації зображення. Опис має бути символічним та мінімалістичним.\n\n"
                f"Текст: {processed_text[:300]}")
            image_prompt = await generate_text_with_fallback(image_prompt_for_ai)
            image_bytes = await generate_image(image_prompt.strip() if image_prompt else "school news")
            post_id = uuid.uuid4().hex[:8]
            context.bot_data[f"manual_post_{post_id}"] = {'text': processed_text, 'photo': image_bytes}
            keyboard = [[InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]]
            caption = f"{processed_text}\n\n---\n*Робити розсилку цієї новини?*"
            await query.delete_message()
            if image_bytes: await context.bot.send_photo(chat_id=query.from_user.id, photo=image_bytes, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=query.from_user.id, text=f"{caption}\n\n(Не вдалося згенерувати зображення)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Помилка при обробці новини через ШІ: {e}")
            await query.edit_message_text(f"❌ Сталася помилка: {e}")
        return ConversationHandler.END
    elif action == 'news_manual':
        await query.edit_message_text("Будь ласка, надішліть фото або відео для цього посту.")
        return WAITING_FOR_MEDIA

async def get_news_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    news_text = context.chat_data.get('news_text')
    photo = update.message.photo[-1].file_id if update.message.photo else None
    video = update.message.video.file_id if update.message.video else None
    if not (photo or video):
        await update.message.reply_text("Будь ласка, надішліть фото або відео.")
        return WAITING_FOR_MEDIA
    post_id = uuid.uuid4().hex[:8]
    context.bot_data[f"manual_post_{post_id}"] = {'text': news_text, 'photo': photo, 'video': video}
    keyboard = [[InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]]
    caption = f"{news_text}\n\n---\n*Робити розсилку цієї новини?*"
    if photo: await update.message.reply_photo(photo=photo, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif video: await update.message.reply_video(video=video, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"Користувач {update.effective_user.id} викликав /cancel.")
    if update.callback_query: await update.callback_query.answer()
    if context.chat_data or context.user_data:
        await update.effective_message.reply_text('Операцію скасовано.', reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        context.chat_data.clear()
        return ConversationHandler.END
    else:
        await update.effective_message.reply_text('Немає активних операцій для скасування.', reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

async def test_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Запускаю тестову перевірку сайту...*")
    site_text = get_all_text_from_website()
    if not site_text:
        await update.message.reply_text("❌ Не вдалося отримати текст з сайту. Перевірте лог на помилки.")
        return
    message = f"✅ Успішно отримано {len(site_text)} символів з сайту.\n\n**Початок тексту:**\n\n{site_text[:500]}..."
    await update.message.reply_text(message)

async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Тестую систему ШІ з резервуванням...*")
    response = await generate_text_with_fallback("Привіт! Скажи 'тест успішний'")
    if response:
        await update.message.reply_text(f"✅ Відповідь від ШІ:\n\n{response}")
    else:
        await update.message.reply_text("❌ Помилка: жоден із сервісів ШІ (Gemini, Cloudflare) не відповів.")

async def test_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Тестую Stability AI API...*")
    try:
        image_bytes = await generate_image("school emblem")
        if image_bytes: await update.message.reply_photo(photo=image_bytes, caption="✅ Тестове зображення успішно згенеровано!")
        else: await update.message.reply_text("❌ Stability AI API повернуло порожню відповідь. Перевірте ключ та баланс кредитів.")
    except Exception as e:
        logger.error(f"Помилка тестування Stability AI API: {e}")
        await update.message.reply_text(f"❌ Помилка Stability AI API: {e}")

async def test_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton("Використати мої дані (тест)", callback_data="test_user_default")], [InlineKeyboardButton("Ввести дані вручну", callback_data="test_user_custom")]]
    await update.message.reply_text("🛠️ **Тестування вхідного повідомлення**\n\n"
        "Оберіть, від імені якого користувача надіслати тестове повідомлення:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECTING_TEST_USER

async def handle_test_user_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == 'test_user_default':
        context.chat_data['test_user_info'] = {'id': query.from_user.id, 'name': await get_admin_name(query.from_user.id)}
        await query.edit_message_text("Добре. Тепер надішліть тестове повідомлення (текст, фото або відео), яке ви хочете перевірити.\n\n/cancel для скасування.")
        return WAITING_FOR_TEST_MESSAGE
    elif choice == 'test_user_custom':
        await query.edit_message_text("Будь ласка, введіть тимчасове **ім'я** користувача для тесту.\n\n/cancel для скасування.", parse_mode='Markdown')
        return WAITING_FOR_TEST_NAME

async def get_test_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['test_user_name'] = update.message.text
    await update.message.reply_text("Ім'я збережено. Тепер введіть тимчасовий **ID** користувача (лише цифри).\n\n/cancel для скасування.", parse_mode='Markdown')
    return WAITING_FOR_TEST_ID

async def get_test_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id_text = update.message.text
    if not user_id_text.isdigit():
        await update.message.reply_text("❌ Помилка: ID має складатися лише з цифр. Спробуйте ще раз.")
        return WAITING_FOR_TEST_ID
    user_id = int(user_id_text)
    user_name = context.chat_data.pop('test_user_name')
    context.chat_data['test_user_info'] = {'id': user_id, 'name': user_name}
    await update.message.reply_text("Дані збережено. Тепер надішліть тестове повідомлення (текст, фото або відео).\n\n/cancel для скасування.")
    return WAITING_FOR_TEST_MESSAGE

async def receive_test_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_info = context.chat_data.get('test_user_info')
    if not user_info:
        await update.message.reply_text("❌ Помилка: дані тестового користувача втрачено. Почніть знову з /testm.")
        return ConversationHandler.END
    message = update.message
    media_type, file_id, user_message = None, None, message.text or message.caption or ""
    if message.photo: media_type, file_id = 'photo', message.photo[-1].file_id
    elif message.video: media_type, file_id = 'video', message.video.file_id
    keyboard = [[InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")], [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    forward_text = (f"📩 **Нове звернення [ТЕСТ]**\n\n"
                    f"**Категорія:** Тест\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{user_message}")
    for admin_id in ADMIN_IDS:
        try:
            if media_type == 'photo': await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif media_type == 'video': await context.bot.send_video(chat_id=admin_id, video=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не вдалося надіслати тестове повідомлення адміну {admin_id}: {e}")
    await update.message.reply_text("✅ Тестове повідомлення надіслано всім адміністраторам.")
    context.chat_data.clear()
    return ConversationHandler.END

async def notify_new_admins(application: Application) -> None:
    notified_admins_doc = await load_data('notified_admins', 'main') or {}
    notified_admins = notified_admins_doc.get('ids', [])
    newly_notified = []
    welcome_text = ("Вітаємо! Тепер ви адміністратор цього бота.\n\n"
        "Ви будете отримувати повідомлення від користувачів. Щоб користуватись адмін-панеллю, введіть команду: /admin\n\n"
        "Якщо вам потрібна повна інструкція, скористайтесь командою /info")
    for admin_id in ADMIN_IDS:
        if admin_id not in notified_admins:
            try:
                await application.bot.send_message(chat_id=admin_id, text=welcome_text)
                newly_notified.append(admin_id)
                logger.info(f"Надіслано привітальне повідомлення новому адміну: {admin_id}")
            except Exception as e:
                logger.error(f"Не вдалося надіслати привітальне повідомлення адміну {admin_id}: {e}")
    if newly_notified:
        all_notified = notified_admins + newly_notified
        await save_data('notified_admins', {'ids': all_notified}, 'main')

async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, command_handler: Callable) -> int:
    user_id = update.effective_user.id
    admin_contacts = await load_data('admin_contacts')
    if str(user_id) in admin_contacts:
        await command_handler(update, context)
        return ConversationHandler.END
    else:
        context.chat_data['next_step_handler'] = command_handler.__name__
        keyboard = [[KeyboardButton("Надіслати мій контакт 👤", request_contact=True)]]
        await update.message.reply_text("Для ідентифікації та коректної роботи сповіщень, будь ласка, поділіться вашим контактом.\n\n"
            "Це потрібно зробити лише один раз.", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
        return WAITING_FOR_ADMIN_CONTACT

async def receive_admin_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    user_id = contact.user_id
    admin_contacts = await load_data('admin_contacts') or {}
    if not isinstance(admin_contacts, dict): admin_contacts = {}
    admin_contacts[str(user_id)] = contact.first_name
    await save_data('admin_contacts', admin_contacts, 'main')
    await update.message.reply_text(f"✅ Дякую, {contact.first_name}! Ваш контакт збережено.", reply_markup=ReplyKeyboardRemove())
    next_handler_name = context.chat_data.get('next_step_handler')
    if next_handler_name:
        handler_map = {'admin_panel': admin_panel, 'info_command': info_command, 'test_message_command': test_message_command}
        handler_to_call = handler_map.get(next_handler_name)
        if handler_to_call:
            if handler_to_call == test_message_command: return await test_message_command(update, context)
            else: await handler_to_call(update, context)
    return ConversationHandler.END

async def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    if db:
        logger.info("Firebase connection successful. Loading data from Firestore.")
        try:
            users_doc_ref = get_collection_ref('users')
            users_docs = await asyncio.to_thread(users_doc_ref.stream)
            user_ids = [int(doc.id) for doc in users_docs if str(doc.id).isdigit()]
            application.bot_data['user_ids'] = set(user_ids)
            anonymous_map_doc = await load_data('anonymous_map', 'main') or {}
            application.bot_data['anonymous_map'] = anonymous_map_doc
        except FirebaseError as e:
            logger.error(f"Failed to load initial data from Firestore: {e}")
            application.bot_data['user_ids'] = set()
            application.bot_data['anonymous_map'] = {}
    else:
        logger.warning("No Firebase connection. Data will not be persistent.")
        application.bot_data['user_ids'] = set()
        application.bot_data['anonymous_map'] = {}
    user_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, start_conversation)],
        states={
            SELECTING_CATEGORY: [CallbackQueryHandler(select_category, pattern='^category_.*$')],
            IN_CONVERSATION: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, continue_conversation)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True, conversation_timeout=3600
    )
    anonymous_conv = ConversationHandler(
        entry_points=[CommandHandler('anonymous', anonymous_command)],
        states={ WAITING_FOR_ANONYMOUS_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_anonymous_message)] },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True
    )
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_broadcast, pattern='^admin_broadcast$')],
        states={
            WAITING_FOR_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_broadcast_message)],
            CONFIRMING_BROADCAST: [
                CallbackQueryHandler(send_broadcast, pattern='^confirm_broadcast$'),
                CallbackQueryHandler(cancel_broadcast, pattern='^cancel_broadcast$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True
    )
    kb_entry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_entry, pattern='^admin_kb_add$')],
        states={
            WAITING_FOR_KB_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_key)],
            WAITING_FOR_KB_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_value)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True
    )
    kb_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_edit, pattern=r'^kb_edit:.*$')],
        states={ WAITING_FOR_KB_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_edit_value)] },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True, conversation_timeout=600
    )
    anonymous_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_anonymous_reply, pattern='^anon_reply:.*$')],
        states={ WAITING_FOR_ANONYMOUS_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_anonymous_reply)] },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True,
    )
    admin_reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_admin_reply, pattern='^ai_reply:.*$'),
            CallbackQueryHandler(start_admin_reply, pattern='^manual_reply:.*$'),
            CallbackQueryHandler(start_anonymous_ai_reply, pattern='^anon_ai_reply:.*$')
        ],
        states={
            WAITING_FOR_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_reply)],
            CONFIRMING_AI_REPLY: [
                CallbackQueryHandler(send_ai_reply_to_user, pattern='^send_ai_reply:.*$'),
                CallbackQueryHandler(send_anonymous_ai_reply_to_user, pattern='^send_anon_ai_reply:.*$'),
                CallbackQueryHandler(cancel, pattern='^cancel_ai_reply$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)], allow_reentry=True, per_message=True
    )
    create_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_news_creation, pattern='^admin_create_news$')],
        states={
            WAITING_FOR_NEWS_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_news_text)],
            CONFIRMING_NEWS_ACTION: [CallbackQueryHandler(handle_news_action, pattern='^news_.*$')],
            WAITING_FOR_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, get_news_media)]
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True
    )
    schedule_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_schedule_news, pattern='^admin_schedule_news$')],
        states={
            WAITING_FOR_SCHEDULE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_text)],
            WAITING_FOR_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_time)],
            CONFIRMING_SCHEDULE_POST: [
                CallbackQueryHandler(confirm_schedule_post, pattern='^confirm_schedule_post$'),
                CallbackQueryHandler(cancel_schedule_post, pattern='^cancel_schedule_post$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True,
    )
    admin_setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", lambda u, c: admin_command_entry(u, c, command_handler=admin_panel)),
            CommandHandler("info", lambda u, c: admin_command_entry(u, c, command_handler=info_command)),
            CommandHandler("testm", lambda u, c: admin_command_entry(u, c, command_handler=test_message_command)),
        ],
        states={
            WAITING_FOR_ADMIN_CONTACT: [MessageHandler(filters.CONTACT & filters.User(ADMIN_IDS), receive_admin_contact)],
            SELECTING_TEST_USER: [CallbackQueryHandler(handle_test_user_choice, pattern='^test_user_.*$')],
            WAITING_FOR_TEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_test_name)],
            WAITING_FOR_TEST_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_test_id)],
            WAITING_FOR_TEST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, receive_test_message)]
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=True
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("faq", faq_command))
    application.add_handler(CommandHandler("testsite", test_site_command))
    application.add_handler(CommandHandler("testai", test_ai_command))
    application.add_handler(CommandHandler("testimage", test_image_command))
    application.add_handler(admin_setup_conv) 
    application.add_handler(MessageHandler(filters.REPLY & filters.User(ADMIN_IDS), handle_admin_direct_reply))
    application.add_handler(CallbackQueryHandler(admin_stats_handler, pattern='^admin_stats$'))
    application.add_handler(CallbackQueryHandler(website_update_handler, pattern='^(broadcast_website|cancel_website_update):.*$'))
    application.add_handler(CallbackQueryHandler(generate_post_from_site, pattern='^admin_generate_post$'))
    application.add_handler(CallbackQueryHandler(handle_post_broadcast_confirmation, pattern='^(confirm_post|cancel_post):.*$'))
    application.add_handler(CallbackQueryHandler(view_kb, pattern='^admin_kb_view$'))
    application.add_handler(CallbackQueryHandler(delete_kb_entry, pattern=r'^kb_delete:.*$'))
    application.add_handler(CallbackQueryHandler(faq_button_handler, pattern='^faq_key:'))
    application.add_handler(CallbackQueryHandler(view_scheduled_posts, pattern='^admin_view_scheduled$'))
    application.add_handler(CallbackQueryHandler(cancel_scheduled_job_button, pattern='^cancel_job:'))
    application.add_handler(broadcast_conv)
    application.add_handler(kb_entry_conv)
    application.add_handler(kb_edit_conv)
    application.add_handler(anonymous_conv)
    application.add_handler(anonymous_reply_conv)
    application.add_handler(admin_reply_conv)
    application.add_handler(create_news_conv)
    application.add_handler(schedule_news_conv)
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    application.add_handler(user_conv)
    await application.initialize()
    web_app = web.Application()
    web_app['ptb_app'] = application
    routes = [
        web.get('/', lambda r: web.FileResponse('./index.html')),
        web.get('/ws', handle_websocket),
        web.post(f'/{TELEGRAM_BOT_TOKEN}', handle_telegram_webhook),
        web.post('/api/init', handle_api_init),
        web.post('/api/login', handle_api_login),
        web.post('/api/sendMessage', handle_send_message_web),
        web.post('/api/stats', lambda r: admin_action_wrapper(r, get_stats_web)),
        web.post('/api/kb/view', lambda r: admin_action_wrapper(r, get_kb_view_web)),
        web.post('/api/broadcast', lambda r: admin_action_wrapper(r, broadcast_web)),
        web.post('/api/admin/conversations', lambda r: admin_action_wrapper(r, get_conversations_web)),
        web.post('/api/admin/suggest_reply', lambda r: admin_action_wrapper(r, suggest_reply_web)),
        web.post('/api/admin/improve_text', lambda r: admin_action_wrapper(r, improve_text_web)),
    ]
    cors = aiohttp_cors.setup(web_app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")})
    for route in routes: cors.add(web_app.router.add_route(route.method, route.path, route.handler))
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await application.start()
    try:
        await application.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
        logger.info(f"Вебхук успішно встановлено на {WEBHOOK_URL}")
        await site.start()
        logger.info(f"Веб-сервер запущено на http://0.0.0.0:{port}")
        kyiv_timezone = pytz.timezone("Europe/Kyiv")
        application.job_queue.run_daily(check_website_for_updates, time=dt_time(hour=9, minute=0, tzinfo=kyiv_timezone))
        application.job_queue.run_once(notify_new_admins, 5)
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.stop()
        await runner.cleanup()
        logger.info("Веб-сервер та фонові задачі зупинено.")
        await application.bot.delete_webhook()
        logger.info("Вебхук видалено.")
        await application.shutdown()
        logger.info("Додаток повністю зупинено.")
if __name__ == '__main__':
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот зупинено вручну.")
