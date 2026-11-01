import json
from pathlib import Path
from pipeline import write_csv


def export(engine, folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);report=engine.report()
    write_csv(folder/'inventory.csv',['sku','warehouse','on_hand','reserved','available','version'],report['inventory'])
    write_csv(folder/'orders.csv',['id','store','external_id','state','total_minor','currency','error','created','updated'],report['orders'])
    issues=[dict(kind='ORDER',id=o['id'],status=o['state'],detail=o['error']) for o in report['orders'] if o['state']=='EXCEPTION']
    issues += [dict(kind='TASK',id=t['id'],status=t['status'],detail=t['error']) for t in report['tasks'] if t['status'] in ('UNKNOWN','FAILED')]
    issues += [dict(kind='SETTLEMENT',id=s['ref'],status=s['status'],detail=s['reasons']) for s in report['settlements'] if s['status']=='EXCEPTION']
    write_csv(folder/'exceptions.csv',['kind','id','status','detail'],issues)
    settlements=[dict(row,external_ref=json.loads(row['payload']).get('external_ref',row['ref']),store=json.loads(row['payload']).get('store','')) for row in report['settlements']]
    write_csv(folder/'settlements.csv',['ref','external_ref','store','order_id','status','reasons','created'],settlements)
    write_csv(folder/'cases.csv',['id','store','external_id','status','note','updated'],report['cases'])
    summary=dict(orders=len(report['orders']),tasks=len(report['tasks']),exception_items=len(issues),
                 settlements=len(report['settlements']),audit_events=report['audit_count'],
                 cases=len(report['cases']),pending_cases=sum(c['status']=='AWAITING_REVIEW' for c in report['cases']),
                 negative_inventory=any(r['on_hand']<0 or r['reserved']<0 or r['available']<0 for r in report['inventory']))
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    return summary
