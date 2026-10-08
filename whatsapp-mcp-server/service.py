"""Private sidecar: owner configuration + read-only, agent-filtered context queries."""
import hmac
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
import api
import whatsapp


class Controller:
    def __init__(self, directory, binary):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.binary = binary
        self.lock = threading.RLock()
        self.process = None
        self.state = 'paused'
        self.qr = None
        self.expires_at = 0
        self.config_path = self.directory / 'settings.json'
        self.config = {'enabled': False, 'allowed_agents': []}
        if self.config_path.exists():
            value = json.loads(self.config_path.read_text())
            self.validate(value)
            self.config = value

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != {'enabled', 'allowed_agents'}:
            raise ValueError('Invalid settings')
        if type(value['enabled']) is not bool or not isinstance(value['allowed_agents'], list):
            raise ValueError('Invalid settings')
        if len(value['allowed_agents']) > 20 or any(not isinstance(a, str) or not re.fullmatch(r'[a-z0-9_-]{1,50}', a) for a in value['allowed_agents']):
            raise ValueError('Invalid agent list')

    def save(self):
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.config))
        temporary.chmod(0o600)
        temporary.replace(self.config_path)

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            self.config['enabled'] = True
            self.save()
            self.state = 'connecting'; self.qr = None
            self.started_at = time.monotonic()
            env = {**os.environ, 'ARC_MANAGED': 'true'}
            try:
                self.process = subprocess.Popen([self.binary], cwd=self.directory, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                self.state = 'error'; raise
            threading.Thread(target=self.consume, args=(self.process,), daemon=True).start()

    def consume(self, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                if self.process is not process:
                    return
                state = event.get('state')
                if state not in {'qr', 'connected', 'disconnected', 'error'}:
                    continue
                self.state = state
                self.qr = event.get('qr_data_url') if state == 'qr' else None
                self.expires_at = event.get('expires_at', 0)
        process.wait()
        with self.lock:
            if self.process is process:
                self.qr = None
                self.state = 'error' if self.config['enabled'] else 'paused'

    def stop(self, persist=True):
        with self.lock:
            if persist:
                self.config['enabled'] = False; self.save()
            process = self.process
            self.process = None; self.qr = None; self.state = 'paused'
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        # Never delete session keys. Pausing is reversible and does not unlink the phone.

    def settings(self, value):
        self.validate(value)
        with self.lock:
            self.config = {'enabled': value['enabled'], 'allowed_agents': sorted(set(value['allowed_agents']))}
            self.save()
        if value['enabled']:
            self.start()
        else:
            self.stop()

    def can_read(self, agent):
        with self.lock:
            return self.config['enabled'] and self.state == 'connected' and agent in self.config['allowed_agents']

    def status(self):
        with self.lock:
            if self.state == 'connecting' and time.monotonic() - self.started_at > 45:
                # Keep the saved preference, but stop a handshake that never becomes ready.
                self.stop(persist=False)
                self.state = 'error'
            result = {**self.config, 'state': self.state, 'qr_data_url': self.qr if time.time() < self.expires_at else None,
                      'ready': False, 'available_chats': 0, 'blocked_chats': 0}
        if result['enabled'] and result['state'] == 'connected':
            try:
                with whatsapp.database() as conn:
                    result['available_chats'] = conn.execute('SELECT count(*) FROM arc_chats WHERE eligible=1').fetchone()[0]
                    result['blocked_chats'] = conn.execute('SELECT count(*) FROM arc_chats WHERE eligible=0').fetchone()[0]
                    result['ready'] = True
            except Exception:
                pass
        return result


class Handler(api.Handler):
    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer '+self.server.token).encode('ascii'))

    def do_GET(self):
        if not self.authorized():
            self.respond(401, {'error': 'Authentication required'}); return
        if self.path == '/health':
            self.respond(200, {'status': 'ok'}); return
        if self.path == '/control/status':
            self.respond(200, self.server.controller.status()); return
        if not self.server.controller.can_read(self.headers.get('X-Arc-Agent', '')):
            self.respond(403, {'error': 'Context access disabled for this agent'}); return
        super().do_GET()

    def do_POST(self):
        if not self.authorized():
            self.respond(401, {'error': 'Authentication required'}); return
        # Only owner controls; never forward arbitrary operations to WhatsApp.
        if self.path not in {'/control/connect', '/control/pause', '/control/settings'}:
            self.respond(405, {'error': 'No WhatsApp mutation endpoint'}); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 4096:
                raise ValueError('Invalid body size')
            raw = self.rfile.read(size)
            value = json.loads(raw) if raw else {}
            if self.path == '/control/connect':
                if value != {}: raise ValueError('Unexpected parameters')
                self.server.controller.start()
            elif self.path == '/control/pause':
                if value != {}: raise ValueError('Unexpected parameters')
                self.server.controller.stop()
            else:
                self.server.controller.settings(value)
            self.respond(200, self.server.controller.status())
        except (ValueError, TypeError):
            self.respond(400, {'error': 'Invalid settings'})
        except Exception:
            self.respond(503, {'error': 'Connector unavailable'})


def make_server(controller, token, address=('127.0.0.1', 8080)):
    if len(token) < 32 or not token.isascii():
        raise ValueError('Private service token required')
    server = ThreadingHTTPServer(address, Handler)
    server.token = token; server.controller = controller
    return server


if __name__ == '__main__':
    os.umask(0o077)
    directory = Path(os.environ.get('ARC_DATA_DIR', '/data'))
    whatsapp.MESSAGES_DB_PATH = directory / 'store/context.db'
    controller = Controller(directory, os.environ.get('ARC_CONNECTOR_BIN', '/usr/local/bin/arc-connector'))
    server = make_server(controller, os.environ.get('ARC_API_TOKEN', ''), (os.environ.get('ARC_BIND', '127.0.0.1'), 8080))
    if controller.config['enabled']:
        controller.start()
    def shutdown(*_):
        controller.stop(persist=False)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        controller.stop(persist=False); server.server_close()
