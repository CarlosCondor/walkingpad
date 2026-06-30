import socket
import os
import sys
import threading
import time
import webbrowser

HOST = "0.0.0.0"
PORT = 5001
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")


def ensure_virtualenv_python():
    if os.path.exists(VENV_PYTHON) and os.path.abspath(sys.executable) != VENV_PYTHON:
        os.execv(VENV_PYTHON, [VENV_PYTHON, *sys.argv])


def open_browser():
    """Open the web browser to the application."""
    url = f"http://127.0.0.1:{PORT}"
    print(f"Opening browser to {url}")
    webbrowser.open_new(url)


def port_is_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def serve_app(app):
    from waitress import serve

    serve(app, host=HOST, port=PORT)


if __name__ == "__main__":
    ensure_virtualenv_python()

    if not port_is_available(HOST, PORT):
        print(f"Port {PORT} is already in use. Stop the other process or change PORT in run.py.")
        raise SystemExit(1)

    print(f"Starting production server with Waitress on http://127.0.0.1:{PORT}")
    from app import app, run_ble_loop_forever

    threading.Thread(target=serve_app, args=(app,), daemon=True).start()
    threading.Timer(2, open_browser).start()

    try:
        print("Starting Bluetooth connection...")
        run_ble_loop_forever()
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Stopping server...")
