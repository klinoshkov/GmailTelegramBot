import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from datetime import datetime
from google.auth.transport.requests import Request
from flask import Flask

# Увеличиваем уровень логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # Изменили на DEBUG
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
CHECK_INTERVAL = 60

# Flask HTTP сервер для мониторинга
app = Flask(__name__)

@app.route('/')
def index():
    return "Бот работает!"

class GmailBot:
    def __init__(self):
        self.application = Application.builder().token(TELEGRAM_TOKEN).build()
        self.setup_handlers()
        self.last_check_time = datetime.now()
        self.known_messages = set()
        self.creds = None  # Добавляем свойство для хранения авторизационных данных
        self.chat_id = None  # Добавляем атрибут для хранения chat_id

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("check", self.check_now))
        self.application.add_error_handler(self.error_handler)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        logger.debug("Получена команда /start")
        self.chat_id = update.effective_chat.id  # Сохраняем chat_id
        await update.message.reply_text(
            'Привет! Я бот, который уведомит тебя о новых письмах.\n'
            'Сейчас попробую подключиться к Gmail...'
        )

        try:
            # Пробуем авторизоваться сразу при старте
            self.creds = self.authorize_google()
            if self.creds:
                await update.message.reply_text('✅ Успешно подключился к Gmail!')
                # Запускаем периодическую проверку почты
                context.application.job_queue.run_repeating(
                    self.check_mail_job, 
                    interval=CHECK_INTERVAL, 
                    first=0,
                    chat_id=self.chat_id
                )
            else:
                await update.message.reply_text('❌ Не удалось подключиться к Gmail')
        except Exception as e:
            logger.error(f"Ошибка при авторизации: {str(e)}")
            await update.message.reply_text(f'❌ Ошибка при подключении к Gmail: {str(e)}')

    async def check_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.debug("Получена команда /check")
        await update.message.reply_text('Проверяю почту...')
        await self.check_mail_job(context)

    def authorize_google(self):
        """Авторизация в Google Gmail API"""
        logger.debug("Начинаем авторизацию Google")
        creds = None

        # Проверяем существование token.json
        if os.path.exists('token.json'):
            logger.debug("Найден существующий token.json")
            try:
                creds = Credentials.from_authorized_user_file('token.json', GMAIL_SCOPES)
            except Exception as e:
                logger.error(f"Ошибка при чтении token.json: {str(e)}")
                creds = None

        # Проверяем валидность credentials
        if not creds or not creds.valid:
            logger.debug("Требуется обновление или новая авторизация")
            if creds and creds.expired and creds.refresh_token:
                logger.debug("Обновляем просроченный токен")
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Ошибка при обновлении токена: {str(e)}")
                    creds = None
            else:
                logger.debug("Запускаем новый процесс авторизации")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', GMAIL_SCOPES)
                    creds = flow.run_local_server(port=0)
                    logger.debug("Авторизация через браузер успешна")
                except Exception as e:
                    logger.error(f"Ошибка при запуске авторизации: {str(e)}")
                    creds = None

            # Сохраняем новые credentials
            try:
                with open('token.json', 'w') as token:
                    token.write(creds.to_json())
                logger.debug("Новый токен сохранен в token.json")
            except Exception as e:
                logger.error(f"Ошибка при сохранении токена: {str(e)}")

        return creds

    async def check_mail_job(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодическая проверка почты"""
        logger.debug("Начинаем проверку почты")
        try:
            if not self.creds:
                self.creds = self.authorize_google()

            if self.creds:
                service = build('gmail', 'v1', credentials=self.creds)
                results = service.users().messages().list(
                    userId='me',
                    maxResults=10,
                    q='is:unread'
                ).execute()

                messages = results.get('messages', [])
                logger.debug(f"Найдено {len(messages)} непрочитанных писем")

                for message in messages:
                    logger.debug(f"Обрабатываем новое письмо: {message['id']}")
                    if message['id'] not in self.known_messages:
                        msg = service.users().messages().get(
                            userId='me',
                            id=message['id']
                        ).execute()

                        headers = msg['payload']['headers']
                        subject = next(
                            (h['value'] for h in headers if h['name'] == 'Subject'),
                            'Без темы'
                        )
                        sender = next(
                            (h['value'] for h in headers if h['name'] == 'From'),
                            'Неизвестный отправитель'
                        )

                        notification_text = (
                            f"📧 Новое письмо!\n"
                            f"От: {sender}\n"
                            f"Тема: {subject}"
                        )

                        if self.chat_id:
                            logger.debug(f"Отправляю уведомление в чат {self.chat_id}")
                            await context.bot.send_message(
                                chat_id=self.chat_id,
                                text=notification_text
                            )

                        self.known_messages.add(message['id'])
            else:
                logger.error("Не удалось получить учетные данные Google")
        except Exception as e:
            logger.error(f"Ошибка при проверке почты: {str(e)}")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Произошла ошибка: {context.error}")

    def run(self):
        """Запуск бота"""
        logger.info("Запускаем бота")
        self.application.run_polling()

if __name__ == '__main__':
    # Запуск Flask сервера
    from threading import Thread
    server = Thread(target=lambda: app.run(host='0.0.0.0', port=8080))
    server.start()

    # Запуск Telegram бота
    bot = GmailBot()
    bot.run()
