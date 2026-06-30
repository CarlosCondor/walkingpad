import socket
import threading
import webbrowser

from waitress import serve

HOST = "0.0.0.0"
PORT = 5001


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


if __name__ == "__main__":
    if not port_is_available(HOST, PORT):
        print(f"Port {PORT} is already in use. Stop the other process or change PORT in run.py.")
        raise SystemExit(1)

    print(f"Starting production server with Waitress on http://127.0.0.1:{PORT}")
    from app import app

    threading.Timer(2, open_browser).start()
    serve(app, host=HOST, port=PORT)
