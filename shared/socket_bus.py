# In separater Datei shared/socket_bus.py
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

class SocketMessageBus:
    def __init__(self):
        self.handlers = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        
    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)
        
    def start_server(self, port=45678):
        def run():
            with socket.socket() as s:
                s.bind(('localhost', port))
                s.listen()
                while True:
                    conn, addr = s.accept()
                    self.executor.submit(self.handle_connection, conn)
        
        threading.Thread(target=run, daemon=True).start()
    
    def handle_connection(self, conn):
        with conn:
            data = conn.recv(4096)
            message = json.loads(data)
            for handler in self.handlers.get(message['type'], []):
                handler(message)
