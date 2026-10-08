import ast
import asyncio
import http.client
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'whatsapp-mcp-server'))
import whatsapp
import api


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'context.db'
        self.old_path = whatsapp.MESSAGES_DB_PATH
        whatsapp.MESSAGES_DB_PATH = self.path
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''
        CREATE TABLE arc_meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE arc_chats(jid TEXT PRIMARY KEY,name TEXT,eligible INTEGER);
        CREATE TABLE arc_messages(id TEXT,chat_jid TEXT,sender TEXT,content TEXT,timestamp INTEGER,is_from_me INTEGER);
        INSERT INTO arc_meta VALUES('policy','read-history-v1');
        INSERT INTO arc_chats VALUES('safe','Alice',1),('blocked','Bob',0);
        INSERT INTO arc_messages VALUES('a','safe','safe','approved',10,0),
          ('b','blocked','blocked','SECRET PENDING',20,0),
          ('c','safe','safe','next approved',30,0);
        ''')
        self.db.execute("INSERT INTO arc_meta VALUES('heartbeat',?)", (str(int(time.time())),))
        self.db.commit()

    def tearDown(self):
        self.db.close(); whatsapp.MESSAGES_DB_PATH = self.old_path; self.temp.cleanup()

    def test_all_query_paths_hide_pending_content(self):
        outputs = [whatsapp.list_messages(), whatsapp.list_messages(query='SECRET'),
                   whatsapp.list_messages(chat_jid='blocked'), whatsapp.list_chats(),
                   whatsapp.get_chat('blocked'), whatsapp.get_message_context('b'),
                   whatsapp.get_contact_chats('blocked'), whatsapp.get_last_interaction('blocked'),
                   whatsapp.search_contacts('Bob')]
        self.assertNotIn('SECRET', repr(outputs))
        self.assertIsNone(whatsapp.get_message_context('b'))
        self.assertEqual(whatsapp.list_messages(chat_jid='blocked'), [])
        chat = whatsapp.get_chat('blocked')
        self.assertNotIn('last_message', chat)
        self.assertEqual(chat['access'], 'blocked_unread_or_unknown')
        self.assertEqual(whatsapp.get_message_context('a')['after'][0]['id'], 'c')

    def test_blocking_existing_context_applies_to_every_query(self):
        self.db.execute('UPDATE arc_chats SET eligible=0'); self.db.commit()
        self.assertEqual(whatsapp.list_messages(), [])
        self.assertIsNone(whatsapp.get_message_context('a'))
        self.assertIsNone(whatsapp.get_last_interaction('safe'))
        self.assertEqual(whatsapp.get_contact_chats('safe'), [])

    def test_stale_and_unknown_stores_fail_closed(self):
        for value in ['0', str(int(time.time())+30)]:
            self.db.execute("UPDATE arc_meta SET value=? WHERE key='heartbeat'",(value,));self.db.commit()
            with self.assertRaises(PermissionError): whatsapp.list_messages()
        self.db.execute("UPDATE arc_meta SET value=? WHERE key='heartbeat'",(str(int(time.time())),))
        self.db.execute("DELETE FROM arc_meta WHERE key='policy'");self.db.commit()
        with self.assertRaises(PermissionError): whatsapp.list_messages()

    def test_database_cannot_write(self):
        with whatsapp.database() as conn:
            with self.assertRaises(sqlite3.OperationalError): conn.execute('DELETE FROM arc_messages')

    def test_injection_and_limits(self):
        self.assertEqual(whatsapp.list_messages(chat_jid="' OR 1=1 --"), [])
        for value in [-1,101,True]:
            with self.assertRaises(ValueError): whatsapp.list_messages(limit=value)
        with self.assertRaises(ValueError): whatsapp.list_messages(after='2026-10-08')

    def test_duplicate_message_id_does_not_cross_chats(self):
        self.db.execute("INSERT INTO arc_messages VALUES('a','blocked','blocked','SECRET',40,0)");self.db.commit()
        self.assertEqual(whatsapp.get_message_context('a')['message']['chat_jid'],'safe')
        self.db.execute("UPDATE arc_chats SET eligible=1 WHERE jid='blocked'");self.db.commit()
        with self.assertRaises(ValueError): whatsapp.get_message_context('a')

    def test_http_has_auth_and_no_writes(self):
        server=api.make_server('x'*32,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection(*server.server_address)
        try:
            conn.request('GET','/api/messages'); self.assertEqual(conn.getresponse().status,401)
            conn.close();conn=http.client.HTTPConnection(*server.server_address)
            conn.request('GET','/api/messages',headers={'Authorization':'Bearer '+'x'*32})
            response=conn.getresponse();self.assertEqual(response.status,200);self.assertNotIn(b'SECRET',response.read())
            for path in ['/api/send','/api/download','/api/messages']:
                conn.request('POST',path,body='{}');response=conn.getresponse()
                self.assertEqual(response.status,405);response.read()
            conn.request('GET','/api/send',headers={'Authorization':'Bearer '+'x'*32})
            response=conn.getresponse();self.assertEqual(response.status,404);response.read()
        finally:
            conn.close();server.shutdown();server.server_close();thread.join()

    def test_mcp_registry_and_calls(self):
        import main
        async def check():
            registered = await main.mcp.list_tools()
            self.assertEqual(len(registered), 8)
            self.assertFalse(any(t.name.startswith(('send', 'download', 'mark')) for t in registered))
            result = await main.mcp.call_tool('list_messages', {'include_context': False})
            self.assertIn('approved', repr(result))
            self.assertNotIn('SECRET', repr(result))
            result = await main.mcp.call_tool('get_message_context', {'message_id': 'b'})
            self.assertNotIn('SECRET', repr(result))
            with self.assertRaises(Exception):
                await main.mcp.call_tool('send_message', {'recipient': '123', 'message': 'blocked'})
        asyncio.run(check())

    def test_application_has_no_mutation_calls(self):
        go=(ROOT/'whatsapp-bridge/main.go').read_text()
        for forbidden in ['.SendMessage(','.MarkRead(','.Upload(','.SendPresence(','.SendChatPresence(','.SendAppState(']:
            self.assertNotIn(forbidden,go)
        tree=ast.parse((ROOT/'whatsapp-mcp-server/main.py').read_text())
        names={node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
        self.assertEqual(names, {'search_contacts','list_messages','list_chats','get_chat',
          'get_direct_chat_by_contact','get_contact_chats','get_last_interaction','get_message_context'})
        tree=ast.parse((ROOT/'whatsapp-mcp-server/whatsapp.py').read_text())
        imports={n.name for node in ast.walk(tree) if isinstance(node,ast.Import) for n in node.names}
        self.assertFalse(imports & {'requests','httpx','socket','subprocess'})


if __name__ == '__main__': unittest.main()
