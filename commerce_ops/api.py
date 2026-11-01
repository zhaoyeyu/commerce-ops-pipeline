"""Loopback JSON interface for scripts, RPA and workflow tools. No browser dashboard."""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from .engine import Engine


def make_server(engine, port=8769, token=''):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):pass
        def respond(self, code, value):
            body=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def authorized(self):
            return bool(token and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token))
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/health':return self.respond(200,dict(status='ok',service='commerce-ops'))
            if not self.authorized():return self.respond(401,dict(error='AUTH_REQUIRED'))
            try:
                if path=='/report':result=engine.report()
                elif path.startswith('/orders/'):result=engine.order(path[len('/orders/'):])
                elif path.startswith('/tasks/'):result=engine.task(path[len('/tasks/'):])
                else:return self.respond(404,dict(error='NOT_FOUND'))
                self.respond(200,result)
            except KeyError:self.respond(404,dict(error='NOT_FOUND'))
        def do_POST(self):
            if not self.authorized():return self.respond(401,dict(error='AUTH_REQUIRED'))
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024:return self.respond(413,dict(error='BODY_SIZE'))
                data=json.loads(self.rfile.read(length));path=urlsplit(self.path).path
                if not isinstance(data,dict):raise ValueError('JSON_OBJECT_REQUIRED')
                if path=='/inventory':
                    engine.stock(data['sku'],data['on_hand'],data.get('warehouse','main'),data['snapshot_id']);result=dict(status='UPDATED')
                elif path=='/orders':result=engine.create_order(data['store'],data['external_id'],data['lines'],data.get('currency','USD'))
                elif path=='/settlements':result=engine.settle(**data)
                elif path=='/cases':result=engine.create_case(**data)
                elif path.startswith('/cases/') and path.endswith('/resolve'):result=engine.resolve_case(path.split('/')[2],data['note'])
                elif path.startswith('/orders/') and path.endswith('/reserve'):result=engine.reserve(path.split('/')[2])
                elif path.startswith('/orders/') and path.endswith('/queue'):result=dict(task_id=engine.queue(path.split('/')[2]))
                elif path.startswith('/orders/') and path.endswith('/cancel'):result=engine.cancel(path.split('/')[2])
                else:return self.respond(404,dict(error='NOT_FOUND'))
                self.respond(200,result)
            except KeyError:self.respond(400,dict(error='REQUIRED_FIELD_OR_ORDER_MISSING'))
            except (ValueError,TypeError) as error:self.respond(409,dict(error=str(error)))
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',default='data/operations.sqlite');parser.add_argument('--port',type=int,default=8769)
    args=parser.parse_args();token=os.environ.get('COMMERCE_OPS_TOKEN','')
    if not token:raise SystemExit('Set COMMERCE_OPS_TOKEN before starting the operator API.')
    server=make_server(Engine(args.db),args.port,token)
    print('Commerce Ops JSON API http://127.0.0.1:'+str(server.server_port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
