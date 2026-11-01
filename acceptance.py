"""Reproducible synthetic operator run, including real HTTP and restart recovery."""
import argparse
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from commerce_ops import Engine
from commerce_ops.api import make_server
from commerce_ops.connectors import HttpConnector,dispatch,reconcile
from commerce_ops.reports import export


def run(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    database=folder/'operations.sqlite'
    if database.exists():raise ValueError('Choose a fresh output directory; existing evidence is preserved.')
    engine=Engine(database);started=time.perf_counter();checks={};remote_effects={};post_count=0
    class Adapter(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value):
            data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            nonlocal post_count
            key=self.headers['Idempotency-Key'];self.rfile.read(int(self.headers['Content-Length']));post_count+=1
            remote_effects.setdefault(key,dict(state='COMPLETED',receipt_id='ERP-RECEIPT-'+key))
            self.reply(dict(state='ACCEPTED'))
        def do_GET(self):self.reply(remote_effects.get(self.path.rsplit('/',1)[-1],dict(state='NOT_FOUND')))
    remote=ThreadingHTTPServer(('127.0.0.1',0),Adapter);api=make_server(engine,0,'acceptance-only-token')
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (remote,api)]
    for thread in threads:thread.start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def post(path,payload):
        req=urllib.request.Request('http://127.0.0.1:'+str(api.server_port)+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer acceptance-only-token','Content-Type':'application/json'})
        with opener.open(req,timeout=10) as response:return json.load(response)
    try:
        post('/inventory',dict(sku='SKU-001',on_hand=5,snapshot_id='stock-v1'))
        payload=dict(store='shop-one',external_id='ORDER-001',lines=[dict(sku='SKU-001',qty=2,unit_price='4.20')])
        order=post('/orders',payload);checks['http_order_reserved']=order['state']=='RESERVED'
        checks['duplicate_http_order_idempotent']=post('/orders',payload)['id']==order['id']
        task=post('/orders/'+order['id']+'/queue',{})['task_id']
        connector=HttpConnector('http://127.0.0.1:'+str(remote.server_port))
        checks['pending_remote_result_retains_stock']=dispatch(engine,task,connector)['status']=='UNKNOWN'
        checks['restart_and_receipt_reconcile']=reconcile(Engine(database),task,connector)['status']=='DONE'
        checks['single_remote_side_effect']=post_count==1 and len(remote_effects)==1
        post('/inventory',dict(sku='SKU-001',on_hand=5,snapshot_id='stock-v1'))
        checks['stock_snapshot_replay_does_not_restock']=engine.report()['inventory'][0]['on_hand']==3
        matched=post('/settlements',dict(ref='PAY-001',store='shop-one',external_id='ORDER-001',gross='8.40',fee='0.40',net='8.00'))
        checks['settlement_matched']=matched['status']=='MATCHED'
        mismatch=post('/settlements',dict(ref='PAY-002',store='shop-one',external_id='ORDER-001',gross='8.40',fee='0.40',net='7.90'))
        checks['settlement_exception_visible']=mismatch['status']=='EXCEPTION'
        engine.stock('LOAD-SKU',100,snapshot_id='load-stock')
        def place(i):
            return Engine(database).create_order('load-shop-'+str(i%4),'LOAD-'+str(i),[dict(sku='LOAD-SKU',qty=1,unit_price='3.50')])['state']
        with ThreadPoolExecutor(max_workers=20) as pool:states=list(pool.map(place,range(500)))
        checks['500_orders_20_workers_no_oversell']=states.count('RESERVED')==100 and states.count('EXCEPTION')==400
        summary=export(engine,folder/'reports');checks['stock_invariants']=not summary['negative_inventory']
        result=dict(dataset='synthetic',operator_transport='real loopback HTTP',adapter_transport='real loopback HTTP',
                    scenarios=checks,passed=all(checks.values()),orders=summary['orders'],audit_events=summary['audit_events'],
                    concurrency_workers=20,load_orders=500,remote_posts=post_count,remote_side_effects=len(remote_effects),seconds=round(time.perf_counter()-started,3))
        (folder/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        if not result['passed']:raise RuntimeError('ACCEPTANCE_FAILED: '+json.dumps(checks))
        return result
    finally:
        for server in (api,remote):server.shutdown();server.server_close()
        for thread in threads:thread.join()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    print(json.dumps(run(a.output),ensure_ascii=False,indent=2))
