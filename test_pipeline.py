import csv
import tempfile
import unittest
from pathlib import Path
from pipeline import run

class PipelineTests(unittest.TestCase):
    def test_validation_rerun_and_price_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {'gross_margin': 0.4, 'usd_rates': {'USD': 1, 'CNY': 0.14}}
            source = Path(__file__).parent/'examples'/'supplier.csv'
            first = run(source, root/'out', root/'ops.sqlite', cfg)
            self.assertEqual((first['ready'], first['exceptions'], first['unchanged']), (2, 2, 0))
            with (root/'out'/'catalog.csv').open(encoding='utf-8-sig') as f:
                products = list(csv.DictReader(f))
            self.assertEqual(products[0]['price_usd'], '4.20')
            second = run(source, root/'out', root/'ops.sqlite', cfg)
            self.assertEqual((second['ready'], second['unchanged']), (0, 2))
            cfg['gross_margin'] = 0.5
            self.assertEqual(run(source, root/'out', root/'ops.sqlite', cfg)['ready'], 2)

    def test_distinct_skus_must_not_overwrite_same_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'collision.csv'
            source.write_text('sku,title,category,cost,currency,stock\nABC,A,C,2,USD,1\nabc,B,C,2,USD,1\n', encoding='utf-8')
            result = run(source, root/'out', root/'ops.sqlite', {'gross_margin':0.4,'usd_rates':{'USD':1}})
            self.assertEqual((result['ready'], result['exceptions']), (1, 1))

if __name__ == '__main__': unittest.main()
