import argparse
import json
from pathlib import Path
from .engine import Engine
from .connectors import HttpConnector,dispatch,reconcile


def main():
    p=argparse.ArgumentParser(description='Commerce operator workflows')
    p.add_argument('--db',default='data/operations.sqlite')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('report')
    x=sub.add_parser('export');x.add_argument('folder')
    x=sub.add_parser('stock');x.add_argument('sku');x.add_argument('on_hand',type=int);x.add_argument('--warehouse',default='main');x.add_argument('--snapshot-id')
    x=sub.add_parser('import-catalog');x.add_argument('file');x.add_argument('--config',default='config.json');x.add_argument('--output',default='output/catalog');x.add_argument('--stock-snapshot-id')
    x=sub.add_parser('import-orders');x.add_argument('file')
    for name in ['reserve','queue','cancel']:
        x=sub.add_parser(name);x.add_argument('id')
    for name in ['dispatch','reconcile']:
        x=sub.add_parser(name);x.add_argument('id');x.add_argument('--connector',required=True)
    x=sub.add_parser('import-settlements');x.add_argument('file')
    x=sub.add_parser('recover');x.add_argument('--before',required=True)
    a=p.parse_args();e=Engine(a.db)
    if a.command=='report':result=e.report()
    elif a.command=='export':
        from .reports import export
        result=export(e,a.folder)
    elif a.command=='stock':e.stock(a.sku,a.on_hand,a.warehouse,a.snapshot_id);result=dict(status='UPDATED')
    elif a.command=='import-catalog':
        from pipeline import run
        result=run(a.file,a.output,a.db,json.loads(Path(a.config).read_text(encoding='utf8')))
        result['stock_updates']=[]
        if a.stock_snapshot_id:
            with e.db() as db:products=[json.loads(r[0]) for r in db.execute('''SELECT DISTINCT p.payload FROM products p
                JOIN events ev ON p.sku=ev.sku WHERE ev.run_id=? AND ev.state IN ('READY','UNCHANGED')''',(result['run_id'],))]
            for product in products:
                try:
                    e.stock(product['sku'],int(product['stock']),snapshot_id=a.stock_snapshot_id)
                    result['stock_updates'].append(dict(sku=product['sku'],status='APPLIED_OR_ALREADY_SEEN'))
                except ValueError as error:result['stock_updates'].append(dict(sku=product['sku'],error=str(error)))
    elif a.command=='import-orders':
        records=json.loads(Path(a.file).read_text(encoding='utf-8-sig'))
        result=[]
        for i,r in enumerate(records):
            try:result.append(dict(index=i,order=e.create_order(**r)))
            except (ValueError,KeyError,TypeError) as error:result.append(dict(index=i,error=str(error)))
    elif a.command=='import-settlements':
        result=[]
        for i,r in enumerate(json.loads(Path(a.file).read_text(encoding='utf-8-sig'))):
            try:result.append(dict(index=i,settlement=e.settle(**r)))
            except (ValueError,KeyError,TypeError) as error:result.append(dict(index=i,error=str(error)))
    elif a.command in ('dispatch','reconcile'):
        import os
        connector=HttpConnector(a.connector,os.environ.get('COMMERCE_CONNECTOR_TOKEN',''))
        result=(dispatch if a.command=='dispatch' else reconcile)(e,a.id,connector)
    elif a.command=='recover':result=dict(unknown_tasks=e.recover(a.before))
    else:result=getattr(e,a.command)(a.id)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
