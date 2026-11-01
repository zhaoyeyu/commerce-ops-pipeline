import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from commerce_ops import Engine
from commerce_ops.api import make_server
from commerce_ops.connectors import HttpConnector,dispatch,reconcile


class HttpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.engine=Engine(Path(self.temp.name)/'ops.sqlite')
        self.server=make_server(self.engine,0,'local-test-token');self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,data=None,auth=True):
        headers={'Authorization':'Bearer local-test-token'} if auth else {}
        body=None if data is None else json.dumps(data).encode()
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,data=body,headers=headers)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req,timeout=5) as r:return json.load(r)
    def test_real_http_import_queue_and_report(self):
        self.assertEqual(self.request('/health',auth=False)['status'],'ok')
        with self.assertRaises(urllib.error.HTTPError) as raised:self.request('/report',auth=False)
        self.assertEqual(raised.exception.code,401)
        self.request('/inventory',dict(sku='A',on_hand=3,snapshot_id='supplier-day1'))
        order=self.request('/orders',dict(store='a',external_id='100',lines=[dict(sku='A',qty=2,unit_price='2.40')]))
        task=self.request('/orders/'+order['id']+'/queue',{})['task_id']
        self.assertEqual(self.request('/tasks/'+task)['status'],'READY')
        self.assertEqual(self.request('/report')['inventory'][0]['available'],1)
    def test_replayed_stock_input_does_not_restock_fulfilled_goods(self):
        e=self.engine;e.stock('A',3,snapshot_id='supplier-day1')
        o=e.create_order('a','1',[dict(sku='A',qty=2,unit_price='1')]);t=e.queue(o['id']);e.claim(t);e.finish(t,'DONE',{'receipt':'ack'})
        e.stock('A',3,snapshot_id='supplier-day1');self.assertEqual(e.report()['inventory'][0]['on_hand'],1)
        with self.assertRaisesRegex(ValueError,'SNAPSHOT_CONFLICT'):e.stock('A',4,snapshot_id='supplier-day1')
    def test_real_remote_ack_lookup_after_uncertain_delivery(self):
        receipts={};posts=[]
        class Remote(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def reply(self,value):
                data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_POST(self):
                key=self.headers['Idempotency-Key'];payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                posts.append(key);receipts.setdefault(key,dict(state='COMPLETED',receipt_id='erp-'+key))
                # Simulate an intermediary losing the acknowledgement after acceptance.
                self.reply(dict(state='ACCEPTED'))
            def do_GET(self):self.reply(receipts.get(self.path.rsplit('/',1)[-1],dict(state='NOT_FOUND')))
        remote=ThreadingHTTPServer(('127.0.0.1',0),Remote);thread=threading.Thread(target=remote.serve_forever,daemon=True);thread.start()
        try:
            e=self.engine;e.stock('A',3);o=e.create_order('a','1',[dict(sku='A',qty=2,unit_price='1')]);task=e.queue(o['id'])
            connector=HttpConnector('http://127.0.0.1:'+str(remote.server_port))
            self.assertEqual(dispatch(e,task,connector)['status'],'UNKNOWN')
            self.assertEqual(reconcile(e,task,connector)['status'],'DONE')
            self.assertEqual(posts,[task]);self.assertEqual(e.report()['inventory'][0]['on_hand'],1)
        finally:remote.shutdown();remote.server_close();thread.join()

if __name__=='__main__':unittest.main()
