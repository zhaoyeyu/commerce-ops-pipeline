"""Supplier catalog -> validated import files, exception queue and audit trail."""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from datetime import datetime, timezone

FIELDS = ['sku', 'title', 'description', 'brand', 'category', 'cost', 'currency', 'stock', 'image_url']
ALIASES = {'sku': ['sku', 'SKU', '货号'], 'title': ['title', '商品名称'],
           'description': ['description', '商品描述'], 'brand': ['brand', '品牌'],
           'category': ['category', '分类'], 'cost': ['cost', '成本'],
           'currency': ['currency', '币种'], 'stock': ['stock', '库存'],
           'image_url': ['image_url', '图片地址']}

def normalize(row, rate, margin):
    item = {key: next((str(row[k]).strip() for k in names if row.get(k) is not None), '')
            for key, names in ALIASES.items()}
    errors = []
    for key in ('sku', 'title', 'category'):
        if not item[key]: errors.append(key + '_required')
    if item['image_url'] and not re.match(r'^https://[^\s]+$', item['image_url']):
        errors.append('image_must_be_https')
    try:
        cost = Decimal(item['cost'])
        if not cost.is_finite() or cost <= 0: raise ValueError()
        if item['currency'] not in rate: raise ValueError()
        usd = cost * Decimal(str(rate[item['currency']]))
        price = (usd / (1 - margin)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        item['price_usd'] = str(price)
    except (InvalidOperation, ValueError, KeyError): errors.append('invalid_cost_or_currency')
    try:
        stock = int(item['stock'])
        if stock < 0: raise ValueError()
        item['stock'] = str(stock)
    except ValueError: errors.append('invalid_stock')
    item['handle'] = re.sub(r'[^a-z0-9_-]', '-', item['sku'].lower()).strip('-')
    if not item['handle']: errors.append('sku_needs_ascii_identifier')
    return item, errors

def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        # Prevent spreadsheet formulas when external supplier fields are opened.
        writer.writerows({k: "'" + str(v) if str(v).startswith(('=', '+', '-', '@')) else v
                          for k, v in row.items()} for row in rows)

def run(source, output, database, config):
    source, output, database = Path(source), Path(output), Path(database)
    margin = Decimal(str(config['gross_margin']))
    rates = config['usd_rates']
    if not 0 <= margin < 1: raise ValueError('gross_margin must be within [0, 1)')
    if not rates or any(not Decimal(str(v)).is_finite() or Decimal(str(v)) <= 0 for v in rates.values()):
        raise ValueError('Exchange rates must be positive finite numbers')
    output.mkdir(parents=True, exist_ok=True)
    database.parent.mkdir(parents=True, exist_ok=True)
    with source.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames: raise ValueError('Empty supplier file')
        rows = list(reader)
    # Include configuration: price-setting changes must trigger an update.
    run_id = hashlib.sha256(source.read_bytes() + json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
    stamp = datetime.now(timezone.utc).isoformat()
    accepted, exceptions, unchanged, seen, handles = [], [], [], set(), set()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, fingerprint TEXT, payload TEXT, updated TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS events (run_id TEXT, line INTEGER, sku TEXT, state TEXT, detail TEXT, created TEXT, PRIMARY KEY(run_id,line))')
        for line, row in enumerate(rows, 2):
            item, errors = normalize(row, rates, margin)
            sku = item['sku']
            if sku and sku in seen: errors.append('duplicate_sku_in_batch')
            if item['handle'] and item['handle'] in handles and sku not in seen: errors.append('duplicate_handle')
            seen.add(sku)
            handles.add(item['handle'])
            payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256(payload.encode()).hexdigest()
            old = db.execute('SELECT fingerprint FROM products WHERE sku=?', (sku,)).fetchone()
            state = 'EXCEPTION' if errors else 'UNCHANGED' if old and old[0] == fingerprint else 'READY'
            db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)',
                       (run_id, line, sku, state, json.dumps(errors), stamp))
            if errors:
                exceptions.append(dict(line=line, sku=sku, title=item['title'], reasons=';'.join(errors)))
            elif state == 'UNCHANGED': unchanged.append(item)
            else:
                db.execute('INSERT OR REPLACE INTO products VALUES(?,?,?,?)', (sku, fingerprint, payload, stamp))
                accepted.append(item)
    write_csv(output/'catalog.csv', FIELDS + ['handle', 'price_usd'], accepted)
    write_csv(output/'exceptions.csv', ['line', 'sku', 'title', 'reasons'], exceptions)
    shopify = [dict(Handle=i['handle'], Title=i['title'], **{'Body (HTML)': i['description'],
                 'Vendor': i['brand'], 'Type': i['category'], 'Published': 'FALSE',
                 'Variant SKU': i['sku'], 'Variant Price': i['price_usd'],
                 'Variant Inventory Qty': i['stock'], 'Image Src': i['image_url'], 'Status': 'draft'}) for i in accepted]
    write_csv(output/'shopify_draft.csv', ['Handle', 'Title', 'Body (HTML)', 'Vendor', 'Type', 'Published',
              'Variant SKU', 'Variant Price', 'Variant Inventory Qty', 'Image Src', 'Status'], shopify)
    manifest = dict(run_id=run_id, input_rows=len(rows), ready=len(accepted), exceptions=len(exceptions),
                    unchanged=len(unchanged), state='IMPORT_FILES_READY', created=stamp)
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source'); parser.add_argument('--config', default='config.json')
    parser.add_argument('--output', default='output'); parser.add_argument('--db', default='data/operations.sqlite')
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output, args.db, json.loads(Path(args.config).read_text(encoding='utf-8')))))
