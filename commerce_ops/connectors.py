"""HTTP adapter contract: durable idempotency keys and independently queryable receipts."""
import json
import urllib.error
import urllib.parse
import urllib.request


class HttpConnector:
    def __init__(self, base_url, token='', timeout=10):
        self.url=base_url.rstrip('/');self.token=token;self.timeout=timeout
        if urllib.parse.urlsplit(self.url).scheme not in ('http','https'):
            raise ValueError('HTTP_CONNECTOR_URL_REQUIRED')

    def request(self, path, payload=None, key=None):
        headers={'Accept':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        if key:headers['Idempotency-Key']=key
        data=None if payload is None else json.dumps(payload).encode()
        if data is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(self.url+path,data=data,headers=headers)
        # The operator supplies the connector URL. Standard configured proxies apply.
        with urllib.request.urlopen(request,timeout=self.timeout) as response:
            result=json.loads(response.read(1024*1024).decode())
        if result.get('state') not in ('COMPLETED','ACCEPTED','NOT_ACCEPTED','NOT_FOUND'):
            raise ValueError('UNRECOGNIZED_REMOTE_RECEIPT')
        if result['state']=='COMPLETED' and not result.get('receipt_id'):
            raise ValueError('REMOTE_RECEIPT_ID_REQUIRED')
        return result

    def send(self, key, payload):
        return self.request('/fulfillments',payload,key)

    def lookup(self, key):
        return self.request('/fulfillments/'+urllib.parse.quote(key,safe=''))


def dispatch(engine, task_id, connector):
    task=engine.claim(task_id)
    order=engine.order(task['order_id'])
    try:
        result=connector.send(task_id,json.loads(order['payload']))
    except Exception as error:
        # A network or HTTP error does not prove whether the remote action happened.
        return engine.finish(task_id,'UNKNOWN',error=type(error).__name__+': '+str(error))
    if result['state']=='COMPLETED':
        return engine.finish(task_id,'DONE',receipt=result)
    if result['state']=='NOT_ACCEPTED':
        return engine.finish(task_id,'RETRY',receipt=result,error=result.get('reason','REMOTE_NOT_ACCEPTED'))
    return engine.finish(task_id,'UNKNOWN',receipt=result,error='REMOTE_PENDING')


def reconcile(engine, task_id, connector):
    task=engine.task(task_id)
    if task['status']!='UNKNOWN':raise ValueError('RECONCILE_UNKNOWN_TASK_ONLY')
    result=connector.lookup(task_id)
    if result['state']=='COMPLETED':return engine.finish(task_id,'DONE',receipt=result)
    # A miss can be replication lag or an in-flight request. It never means resend.
    return dict(task,observation=result)
