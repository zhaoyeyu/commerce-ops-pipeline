import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from commerce_ops import Engine


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ops.sqlite';self.e=Engine(self.path)
        self.e.stock('SKU-A',5)
    def tearDown(self): self.temp.cleanup()
    def create(self,number='1',qty=2,store='store-a'):
        return self.e.create_order(store,number,[dict(sku='SKU-A',qty=qty,unit_price='12.35')])
    def test_order_repeat_and_conflicting_payload(self):
        first=self.create();self.assertEqual(self.create()['id'],first['id'])
        with self.assertRaisesRegex(ValueError,'IDEMPOTENCY'):self.create(qty=3)
        self.assertEqual(self.e.report()['inventory'][0]['reserved'],2)
    def test_store_is_part_of_order_identity(self):
        self.assertNotEqual(self.create(store='one')['id'],self.create(store='two')['id'])
    def test_all_lines_reserve_or_no_lines_reserve(self):
        order=self.e.create_order('one','1',[dict(sku='SKU-A',qty=2,unit_price='1'),dict(sku='MISSING',qty=1,unit_price='1')])
        self.assertEqual(order['state'],'EXCEPTION');self.assertEqual(self.e.report()['inventory'][0]['reserved'],0)
        self.e.stock('MISSING',1);self.assertEqual(self.e.reserve(order['id'])['state'],'RESERVED')
    def test_concurrent_stores_cannot_oversell_shared_stock(self):
        def place(i): return Engine(self.path).create_order(str(i),'1',[dict(sku='SKU-A',qty=1,unit_price='1')])['state']
        with ThreadPoolExecutor(max_workers=8) as pool: states=list(pool.map(place,range(20)))
        self.assertEqual(states.count('RESERVED'),5);self.assertEqual(self.e.report()['inventory'][0]['available'],0)
    def test_duplicate_line_merge_and_price_conflict(self):
        lines=[dict(sku='SKU-A',qty=1,unit_price='1.20')]*2
        self.assertEqual(self.e.create_order('a','1',lines)['total_minor'],240)
        lines[1]=dict(sku='SKU-A',qty=1,unit_price='1.21')
        with self.assertRaisesRegex(ValueError,'PRICE_CONFLICT'): self.e.create_order('a','2',lines)
    def test_completed_remote_receipt_consumes_stock_exactly_once(self):
        order=self.create();task=self.e.queue(order['id']);self.e.claim(task)
        self.e.finish(task,'DONE',dict(remote_id='receipt-1'))
        stock=self.e.report()['inventory'][0];self.assertEqual((stock['on_hand'],stock['reserved']),(3,0))
        with self.assertRaises(ValueError):self.e.finish(task,'DONE',dict(remote_id='receipt-1'))
        self.assertEqual(Engine(self.path).order(order['id'])['state'],'FULFILLED')
    def test_unknown_task_cannot_be_reclaimed_or_cancelled(self):
        order=self.create();task=self.e.queue(order['id']);self.e.claim(task);self.e.finish(task,'UNKNOWN',error='timeout')
        with self.assertRaisesRegex(ValueError,'NOT_CLAIMABLE'):self.e.claim(task)
        with self.assertRaisesRegex(ValueError,'RECONCILE'):self.e.cancel(order['id'])
        self.e.finish(task,'DONE',dict(remote_id='remote-accepted-before-timeout'))
        self.assertEqual(self.e.report()['inventory'][0]['on_hand'],3)
    def test_process_crash_recovery_preserves_uncertainty(self):
        order=self.create();task=self.e.queue(order['id']);self.e.claim(task)
        self.assertEqual(Engine(self.path).recover('2099-01-01T00:00:00+00:00'),1)
        self.assertEqual(self.e.task(task)['status'],'UNKNOWN')
    def test_known_rejection_can_retry_but_exhausted_task_needs_operator(self):
        task=self.e.queue(self.create()['id'])
        for _ in range(5):self.e.claim(task);self.e.finish(task,'RETRY',error='remote explicitly did not accept')
        self.assertEqual(self.e.task(task)['status'],'FAILED')
        with self.assertRaises(ValueError):self.e.claim(task)
    def test_cancel_releases_once_and_prevents_dispatch(self):
        order=self.create();task=self.e.queue(order['id']);self.e.cancel(order['id']);self.e.cancel(order['id'])
        self.assertEqual(self.e.report()['inventory'][0]['reserved'],0)
        with self.assertRaises(ValueError):self.e.claim(task)
    def test_stock_snapshot_cannot_erase_existing_reservation(self):
        self.create()
        with self.assertRaisesRegex(ValueError,'BELOW_RESERVATIONS'):self.e.stock('SKU-A',1)
    def test_money_precision_and_invalid_quantities(self):
        for value in ['NaN','Infinity','-1','0.001']:
            with self.assertRaises(ValueError):self.e.create_order('a',value,[dict(sku='SKU-A',qty=1,unit_price=value)])
        for qty in [0,-1,True,1.5]:
            with self.assertRaises(ValueError): self.create(str(qty),qty)
    def test_settlement_gross_fee_currency_and_dedup(self):
        self.create();row=self.e.settle('p1','store-a','1','24.70','0.70','24.00');self.assertEqual(row['status'],'MATCHED')
        self.assertEqual(self.e.settle('p1','store-a','1','24.70','0.70','24.00')['status'],'MATCHED')
        with self.assertRaisesRegex(ValueError,'CONFLICT'):self.e.settle('p1','store-a','1','24.70','0.70','23.00')
        self.assertIn('FEE_NET_MISMATCH',self.e.settle('p2','store-a','1','24.70','0.70','23.00')['reasons'])
        self.assertIn('CURRENCY_MISMATCH',self.e.settle('p3','store-a','1','24.70','0.70','24.00','EUR')['reasons'])
        self.assertIn('ORDER_NOT_FOUND',self.e.settle('p4','store-a','missing','1','0','1')['reasons'])
        other=self.e.settle('p1','other-store','missing','1','0','1')
        self.assertIn('ORDER_NOT_FOUND',other['reasons'])

    def test_ai_cancel_ticket_creates_review_case_without_changing_order(self):
        order=self.create();classification=dict(intent='CANCEL_REQUEST',next_action='OPERATOR_REVIEW',external_order_id='1',summary='取消请求',reply_draft='先确认状态')
        case=self.e.create_case('store-a','ticket-1','请取消订单1',classification)
        self.assertEqual(case['status'],'AWAITING_REVIEW');self.assertEqual(self.e.order(order['id'])['state'],'RESERVED')
        self.assertEqual(self.e.create_case('store-a','ticket-1','请取消订单1',classification)['id'],case['id'])
        other=self.e.create_case('other-store','ticket-1','请取消订单1',classification)
        self.assertIn('false',other['context'])
        self.e.cancel(order['id']);self.e.resolve_case(case['id'],'已核对仓库未发货并取消预留')
        resolved=next(c for c in self.e.report()['cases'] if c['id']==case['id'])
        self.assertEqual(resolved['status'],'RESOLVED')
        self.assertEqual(resolved['note'],'已核对仓库未发货并取消预留')
    def test_ai_cannot_request_direct_cancellation(self):
        with self.assertRaisesRegex(ValueError,'OPERATOR'):
            self.e.create_case('a','1','取消',dict(intent='CANCEL_REQUEST',next_action='LOOKUP_ORDER'))

if __name__=='__main__': unittest.main()
