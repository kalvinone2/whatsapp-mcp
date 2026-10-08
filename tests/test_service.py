import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'whatsapp-mcp-server'))
import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.controller=service.Controller(self.temp.name,'/no/connector')
        self.server=service.make_server(self.controller,'x'*32,('127.0.0.1',0))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def call(self, method, path, value=None, authorized=True):
        conn=http.client.HTTPConnection(*self.server.server_address)
        headers={'Content-Type':'application/json'}
        if authorized:headers['Authorization']='Bearer '+'x'*32
        conn.request(method,path,body=json.dumps(value) if value is not None else None,headers=headers)
        response=conn.getresponse();status=response.status;data=json.loads(response.read());conn.close()
        return status,data

    def test_default_is_disabled_and_no_agents(self):
        status,value=self.call('GET','/control/status')
        self.assertEqual(status,200);self.assertFalse(value['enabled']);self.assertEqual(value['allowed_agents'],[])
        self.assertIsNone(value['qr_data_url']);self.assertFalse(value['ready'])
        self.assertEqual(self.call('GET','/api/messages')[0],403)

    def test_controls_require_auth_and_reject_mutations(self):
        self.assertEqual(self.call('POST','/control/connect',{},False)[0],401)
        with patch.object(self.controller,'start') as start:
            self.assertEqual(self.call('POST','/control/connect',{})[0],200);start.assert_called_once()
        for path in ['/api/send','/api/download','/control/logout','/control/mark_read']:
            self.assertEqual(self.call('POST',path,{})[0],405)

    def test_permissions_persist_without_starting_connector(self):
        value={'enabled':False,'allowed_agents':['hermes','hermes-rabbit']}
        self.assertEqual(self.call('POST','/control/settings',value)[0],200)
        reopened=service.Controller(self.temp.name,'/no/connector')
        self.assertEqual(reopened.config,value)
        self.controller.state='connected';self.controller.config['enabled']=True
        self.assertTrue(self.controller.can_read('hermes'));self.assertFalse(self.controller.can_read('codex'))
        self.controller.state='disconnected';self.assertFalse(self.controller.can_read('hermes'))
        self.assertEqual(self.call('POST','/control/settings',{'enabled':False,'allowed_agents':['../secret']})[0],400)
        self.assertEqual(self.call('POST','/control/settings',{**value,'token':'secret'})[0],400)

    def test_pause_preserves_session_files(self):
        store=Path(self.temp.name)/'store';store.mkdir();session=store/'whatsapp.db';session.write_bytes(b'synthetic session')
        self.controller.qr='secret QR';self.controller.state='qr';self.controller.config['enabled']=True
        self.assertEqual(self.call('POST','/control/pause',{})[0],200)
        self.assertEqual(session.read_bytes(),b'synthetic session');self.assertIsNone(self.controller.qr)
        self.assertFalse(self.controller.config['enabled'])

    def test_expired_qr_is_never_returned(self):
        self.controller.qr='secret QR';self.controller.expires_at=0
        self.assertIsNone(self.controller.status()['qr_data_url'])

    def test_stalled_handshake_stops_and_closes_access(self):
        self.controller.state='connecting';self.controller.started_at=time.monotonic()-46
        self.controller.config={'enabled':True,'allowed_agents':['hermes']}
        value=self.controller.status()
        self.assertEqual(value['state'],'error');self.assertIsNone(value['qr_data_url'])
        self.assertFalse(value['ready']);self.assertFalse(self.controller.can_read('hermes'))
        self.assertTrue(self.controller.config['enabled'])
