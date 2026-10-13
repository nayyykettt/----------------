"""Запуск планировщика как отдельного приложения: сервер в фоне + нативное окно."""
import os
import shutil
import threading
from http.server import ThreadingHTTPServer

import webview

# Данные — в ~/Library/Application Support, чтобы пересборка .app их не затирала.
DATA_DIR = os.path.expanduser('~/Library/Application Support/Погодный планировщик')
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, 'data.sqlite3')
# Первый запуск: забираем копию данных из папки проекта, если она указана.
OLD_DB = os.environ.get('PLANNER_IMPORT_DB')
if not os.path.exists(DB) and OLD_DB and os.path.exists(OLD_DB):
    shutil.copy2(OLD_DB, DB)
os.environ['PLANNER_DB'] = DB

import server  # noqa: E402 — DB_PATH читается при импорте

server.init_db()
srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)  # свободный порт
threading.Thread(target=srv.serve_forever, daemon=True).start()

webview.create_window('Погодный планировщик', f'http://127.0.0.1:{srv.server_port}/',
                      width=1400, height=900, min_size=(900, 600))
webview.start()
