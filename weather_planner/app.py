"""Приложение для macOS, Windows, Linux, iOS и Android (Briefcase + Toga): сервер планировщика работает
на самом устройстве, окно показывает его страницу во встроенном WebView. Наружу ничего не открывается."""
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import toga
from toga.style import Pack


class Planner(toga.App):
    def startup(self):
        # На macOS — та же папка, что у сборки build_app.sh, чтобы обе версии видели одни данные.
        data = (Path('~/Library/Application Support/Погодный планировщик').expanduser()
                if sys.platform == 'darwin' else Path(self.paths.data))
        data.mkdir(parents=True, exist_ok=True)
        os.environ['PLANNER_DB'] = str(data / 'data.sqlite3')
        try:  # на Android, iOS и Windows у Python нет системных корневых сертификатов для HTTPS к Open-Meteo
            import certifi
            os.environ.setdefault('SSL_CERT_FILE', certifi.where())
        except ImportError:
            pass

        import server  # DB_PATH читается при импорте
        server.init_db()
        srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)  # только этот компьютер, свободный порт
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        self.main_window = toga.MainWindow(title=self.formal_name, size=(1400, 900))
        self.main_window.content = toga.WebView(url=f'http://127.0.0.1:{srv.server_port}/', style=Pack(flex=1))
        self.main_window.show()


def main():
    return Planner()
