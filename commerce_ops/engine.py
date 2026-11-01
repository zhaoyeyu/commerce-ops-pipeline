"""Transactional orders, stock, connector tasks and settlement reconciliation."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number * 100 != (number * 100).to_integral_value():
            raise ValueError('INVALID_MONEY')
        return int(number * 100)
    except (InvalidOperation, TypeError):
        raise ValueError('INVALID_MONEY') from None


def quantity(value):
    if type(value) is not int or value <= 0:
        raise ValueError('POSITIVE_INTEGER_QUANTITY_REQUIRED')
    return value


class Engine:
    def __init__(self, database):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS inventory(sku TEXT,warehouse TEXT,on_hand INTEGER NOT NULL,
                reserved INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(sku,warehouse),CHECK(on_hand>=0),CHECK(reserved>=0 AND reserved<=on_hand));
              CREATE TABLE IF NOT EXISTS stock_inputs(id TEXT PRIMARY KEY,request_hash TEXT,created TEXT);
              CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,store TEXT,external_id TEXT,
                request_hash TEXT,payload TEXT,state TEXT,total_minor INTEGER,currency TEXT,
                error TEXT,created TEXT,updated TEXT,UNIQUE(store,external_id));
              CREATE TABLE IF NOT EXISTS order_lines(order_id TEXT,sku TEXT,warehouse TEXT,qty INTEGER,
                unit_minor INTEGER,PRIMARY KEY(order_id,sku,warehouse));
              CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,order_id TEXT UNIQUE,status TEXT,
                attempts INTEGER DEFAULT 0,receipt TEXT,error TEXT,started TEXT,updated TEXT);
              CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY,entity TEXT,event TEXT,
                detail TEXT,created TEXT);
              CREATE TABLE IF NOT EXISTS settlements(ref TEXT PRIMARY KEY,request_hash TEXT,
                order_id TEXT,payload TEXT,status TEXT,reasons TEXT,created TEXT);
              CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,store TEXT,external_id TEXT,
                request_hash TEXT,ticket TEXT,classification TEXT,context TEXT,status TEXT,note TEXT,updated TEXT);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def audit(db, entity, event, detail):
        db.execute('INSERT INTO audit(entity,event,detail,created) VALUES(?,?,?,?)',
                   (entity, event, encode(detail), stamp()))

    def stock(self, sku, on_hand, warehouse='main', snapshot_id=None):
        if not isinstance(sku,str) or not sku.strip() or not isinstance(warehouse,str) or not warehouse.strip() or type(on_hand) is not int or on_hand < 0:
            raise ValueError('INVALID_STOCK')
        if snapshot_id is not None and (not isinstance(snapshot_id,str) or not snapshot_id.strip()):
            raise ValueError('STOCK_SNAPSHOT_ID_REQUIRED')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            identity=fingerprint([sku,warehouse,snapshot_id]) if snapshot_id else None
            sha=fingerprint([sku,warehouse,on_hand])
            if identity:
                prior=db.execute('SELECT request_hash FROM stock_inputs WHERE id=?',(identity,)).fetchone()
                if prior:
                    if prior[0]!=sha:raise ValueError('STOCK_SNAPSHOT_CONFLICT')
                    return
            old = db.execute('SELECT reserved FROM inventory WHERE sku=? AND warehouse=?', (sku, warehouse)).fetchone()
            if old and on_hand < old['reserved']:
                raise ValueError('STOCK_BELOW_RESERVATIONS')
            db.execute('''INSERT INTO inventory(sku,warehouse,on_hand) VALUES(?,?,?)
              ON CONFLICT(sku,warehouse) DO UPDATE SET on_hand=excluded.on_hand,version=inventory.version+1''',
                       (sku, warehouse, on_hand))
            self.audit(db, sku, 'STOCK_SNAPSHOT', dict(warehouse=warehouse, on_hand=on_hand))
            if identity:db.execute('INSERT INTO stock_inputs VALUES(?,?,?)',(identity,sha,stamp()))

    def create_order(self, store, external_id, lines, currency='USD'):
        if not isinstance(store,str) or not isinstance(external_id,str) or not store or not external_id or currency not in ('USD', 'CNY', 'EUR', 'GBP', 'HKD') or not isinstance(lines,list) or not lines:
            raise ValueError('ORDER_FIELDS_REQUIRED')
        merged = {}
        for item in lines:
            sku = str(item['sku']).strip()
            warehouse = str(item.get('warehouse', 'main')).strip()
            if not sku or not warehouse:
                raise ValueError('LINE_IDENTITY_REQUIRED')
            key = (sku, warehouse)
            price, count = money(item['unit_price']), quantity(item['qty'])
            if key in merged and merged[key]['unit_minor'] != price:
                raise ValueError('DUPLICATE_LINE_PRICE_CONFLICT')
            merged.setdefault(key, dict(sku=sku, warehouse=warehouse, qty=0, unit_minor=price))['qty'] += count
        normalized = sorted(merged.values(), key=lambda x: (x['sku'], x['warehouse']))
        payload = dict(store=store, external_id=external_id, lines=normalized, currency=currency)
        sha = fingerprint(payload)
        key = 'ord_' + fingerprint([store, external_id])[:24]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
            if old:
                if old['request_hash'] != sha:
                    raise ValueError('ORDER_IDEMPOTENCY_CONFLICT')
                return dict(old)
            created = stamp()
            db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (key, store, external_id, sha, encode(payload), 'NEW',
                        sum(i['qty'] * i['unit_minor'] for i in normalized), currency, None, created, created))
            db.executemany('INSERT INTO order_lines VALUES(?,?,?,?,?)',
                           [(key, i['sku'], i['warehouse'], i['qty'], i['unit_minor']) for i in normalized])
            self.audit(db, key, 'ORDER_RECEIVED', dict(store=store, external_id=external_id))
            self._reserve(db, key)
        return self.order(key)

    def _reserve(self, db, key):
        order = db.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
        if not order:
            raise KeyError(key)
        if order['state'] not in ('NEW', 'EXCEPTION'):
            return
        task = db.execute('SELECT status FROM tasks WHERE order_id=?', (key,)).fetchone()
        if task:
            raise ValueError('RESOLVE_EXISTING_FULFILLMENT_TASK_FIRST')
        lines = db.execute('SELECT * FROM order_lines WHERE order_id=?', (key,)).fetchall()
        issues = []
        for item in lines:
            row = db.execute('SELECT * FROM inventory WHERE sku=? AND warehouse=?', (item['sku'], item['warehouse'])).fetchone()
            if not row or row['on_hand'] - row['reserved'] < item['qty']:
                issues.append(dict(sku=item['sku'], warehouse=item['warehouse'], required=item['qty'],
                                   available=0 if not row else row['on_hand']-row['reserved']))
        if issues:
            db.execute("UPDATE orders SET state='EXCEPTION',error=?,updated=? WHERE id=?", (encode(issues), stamp(), key))
            self.audit(db, key, 'RESERVATION_FAILED', issues)
            return
        for item in lines:
            db.execute('UPDATE inventory SET reserved=reserved+?,version=version+1 WHERE sku=? AND warehouse=?',
                       (item['qty'], item['sku'], item['warehouse']))
        db.execute("UPDATE orders SET state='RESERVED',error=NULL,updated=? WHERE id=?", (stamp(), key))
        self.audit(db, key, 'STOCK_RESERVED', [dict(i) for i in lines])

    def reserve(self, key):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._reserve(db, key)
        return self.order(key)

    def queue(self, key):
        task_id = 'fulfill_' + key
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            order = db.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
            if not order or order['state'] != 'RESERVED':
                raise ValueError('ORDER_NOT_RESERVED')
            db.execute("INSERT OR IGNORE INTO tasks(id,order_id,status,updated) VALUES(?,?,'READY',?)", (task_id, key, stamp()))
            self.audit(db, key, 'FULFILLMENT_QUEUED', dict(task_id=task_id))
        return task_id

    def claim(self, task_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] not in ('READY', 'RETRY'):
                raise ValueError('TASK_NOT_CLAIMABLE')
            if row['attempts'] >= 5:
                raise ValueError('TASK_ATTEMPTS_EXHAUSTED')
            db.execute("UPDATE tasks SET status='RUNNING',attempts=attempts+1,started=?,updated=? WHERE id=?", (stamp(), stamp(), task_id))
            self.audit(db, row['order_id'], 'FULFILLMENT_STARTED', dict(task_id=task_id))
        return self.task(task_id)

    def finish(self, task_id, status, receipt=None, error=None):
        if status not in ('DONE', 'RETRY', 'UNKNOWN', 'FAILED'):
            raise ValueError('INVALID_TASK_RESULT')
        if status == 'DONE' and not receipt:
            raise ValueError('REMOTE_RECEIPT_REQUIRED')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task or task['status'] not in ('RUNNING', 'UNKNOWN'):
                raise ValueError('RESULT_REQUIRES_RUNNING_OR_UNKNOWN_TASK')
            if status == 'RETRY' and task['attempts'] >= 5:
                status = 'FAILED'
            key = task['order_id']
            if status == 'DONE':
                order = db.execute('SELECT state FROM orders WHERE id=?', (key,)).fetchone()
                if order['state'] != 'RESERVED':
                    raise ValueError('RESERVATION_NOT_HELD')
                for item in db.execute('SELECT * FROM order_lines WHERE order_id=?', (key,)).fetchall():
                    db.execute('UPDATE inventory SET on_hand=on_hand-?,reserved=reserved-?,version=version+1 WHERE sku=? AND warehouse=?',
                               (item['qty'], item['qty'], item['sku'], item['warehouse']))
                db.execute("UPDATE orders SET state='FULFILLED',updated=? WHERE id=?", (stamp(), key))
            db.execute('UPDATE tasks SET status=?,receipt=?,error=?,updated=? WHERE id=?',
                       (status, encode(receipt) if receipt else None, error, stamp(), task_id))
            self.audit(db, key, 'FULFILLMENT_' + status, dict(task_id=task_id, receipt=receipt, error=error))
        return self.task(task_id)

    def recover(self, older_than):
        """An expired process lease is an uncertain remote outcome, never a retry."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,order_id FROM tasks WHERE status='RUNNING' AND started<?", (older_than,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='UNKNOWN',error='WORKER_LEASE_EXPIRED',updated=? WHERE id=?", (stamp(), row['id']))
                self.audit(db, row['order_id'], 'FULFILLMENT_UNKNOWN', dict(task_id=row['id'], reason='WORKER_LEASE_EXPIRED'))
        return len(rows)

    def cancel(self, key):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM orders WHERE id=?', (key,)).fetchone()
            if not row:
                raise KeyError(key)
            if row['state'] == 'CANCELLED':
                return self.order(key)
            if row['state'] == 'FULFILLED':
                raise ValueError('FULFILLED_ORDER_REQUIRES_RETURN_WORKFLOW')
            task = db.execute('SELECT status FROM tasks WHERE order_id=?', (key,)).fetchone()
            if task and task['status'] in ('RUNNING', 'UNKNOWN'):
                raise ValueError('RECONCILE_BEFORE_CANCEL')
            if row['state'] == 'RESERVED':
                for item in db.execute('SELECT * FROM order_lines WHERE order_id=?', (key,)).fetchall():
                    db.execute('UPDATE inventory SET reserved=reserved-?,version=version+1 WHERE sku=? AND warehouse=?',
                               (item['qty'], item['sku'], item['warehouse']))
            db.execute("UPDATE tasks SET status='CANCELLED',updated=? WHERE order_id=?", (stamp(), key))
            db.execute("UPDATE orders SET state='CANCELLED',updated=? WHERE id=?", (stamp(), key))
            self.audit(db, key, 'ORDER_CANCELLED', {})
        return self.order(key)

    def settle(self, ref, store, external_id, gross, fee, net, currency='USD'):
        if not isinstance(ref,str) or not ref or not isinstance(store,str) or not store:raise ValueError('SETTLEMENT_IDENTITY_REQUIRED')
        payload = dict(external_ref=ref,store=store, external_id=external_id, gross_minor=money(gross), fee_minor=money(fee), net_minor=money(net), currency=currency)
        sha = fingerprint(payload)
        ref='settle_'+fingerprint([store,ref])[:24]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM settlements WHERE ref=?', (ref,)).fetchone()
            if old:
                if old['request_hash'] != sha:
                    raise ValueError('SETTLEMENT_REFERENCE_CONFLICT')
                return dict(old)
            order = db.execute('SELECT * FROM orders WHERE store=? AND external_id=?', (store, external_id)).fetchone()
            reasons = []
            if not order:
                reasons.append('ORDER_NOT_FOUND')
            else:
                if order['currency'] != currency: reasons.append('CURRENCY_MISMATCH')
                if order['total_minor'] != payload['gross_minor']: reasons.append('GROSS_MISMATCH')
            if payload['gross_minor'] - payload['fee_minor'] != payload['net_minor']:
                reasons.append('FEE_NET_MISMATCH')
            status = 'EXCEPTION' if reasons else 'MATCHED'
            db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?,?)',
                       (ref, sha, order['id'] if order else None, encode(payload), status, encode(reasons), stamp()))
            self.audit(db, ref, 'SETTLEMENT_' + status, dict(reasons=reasons))
            return dict(db.execute('SELECT * FROM settlements WHERE ref=?', (ref,)).fetchone())

    def order(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
            if not row: raise KeyError(key)
            return dict(row)

    def create_case(self, store, external_id, ticket, classification):
        if not all(isinstance(x,str) and x for x in (store,external_id,ticket)):
            raise ValueError('CASE_FIELDS_REQUIRED')
        if classification.get('intent') not in ('ORDER_STATUS','CANCEL_REQUEST','PRODUCT_QUESTION','OTHER'):
            raise ValueError('CASE_INTENT')
        if classification.get('next_action') not in ('LOOKUP_ORDER','OPERATOR_REVIEW','LOOKUP_PRODUCT','CLARIFY'):
            raise ValueError('CASE_NEXT_ACTION')
        if classification['intent']=='CANCEL_REQUEST' and classification['next_action']!='OPERATOR_REVIEW':
            raise ValueError('CANCELLATION_REQUIRES_OPERATOR')
        key='case_'+fingerprint([store,external_id])[:24];sha=fingerprint([ticket,classification])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM cases WHERE id=?',(key,)).fetchone()
            if prior:
                if prior['request_hash']!=sha:raise ValueError('CASE_IDEMPOTENCY_CONFLICT')
                return dict(prior)
            order=db.execute('SELECT id,state,currency,total_minor FROM orders WHERE store=? AND external_id=?',
                             (store,classification.get('external_order_id',''))).fetchone()
            context=dict(order) if order else dict(order_found=False)
            db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (key,store,external_id,sha,ticket,encode(classification),encode(context),'AWAITING_REVIEW',None,stamp()))
            self.audit(db,key,'CASE_TRIAGED',dict(intent=classification['intent'],next_action=classification['next_action']))
            return dict(db.execute('SELECT * FROM cases WHERE id=?',(key,)).fetchone())

    def resolve_case(self, key, note):
        if not isinstance(note,str) or not note.strip():raise ValueError('OPERATOR_RESOLUTION_NOTE_REQUIRED')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM cases WHERE id=?',(key,)).fetchone()
            if not row:raise KeyError(key)
            if row['status']=='RESOLVED':return dict(row)
            db.execute("UPDATE cases SET status='RESOLVED',note=?,updated=? WHERE id=?",(note,stamp(),key))
            self.audit(db,key,'CASE_RESOLVED',dict(note=note))
            return dict(db.execute('SELECT * FROM cases WHERE id=?',(key,)).fetchone())

    def task(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (key,)).fetchone()
            if not row: raise KeyError(key)
            return dict(row)

    def report(self):
        with self.db() as db:
            return dict(inventory=[dict(r) for r in db.execute('SELECT *,on_hand-reserved AS available FROM inventory ORDER BY sku,warehouse')],
                        orders=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY created,id')],
                        tasks=[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY updated,id')],
                        settlements=[dict(r) for r in db.execute('SELECT * FROM settlements ORDER BY created,ref')],
                        cases=[dict(r) for r in db.execute('SELECT * FROM cases ORDER BY updated,id')],
                        audit_count=db.execute('SELECT count(*) FROM audit').fetchone()[0])
