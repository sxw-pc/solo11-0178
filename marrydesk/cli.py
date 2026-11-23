"""Independent subsystem commands and replayable offline demo."""
import argparse, json
from pathlib import Path
from . import storage
from . import audit_capture
from . import applicant, slot, visit

HANDLERS={'applicant':applicant.apply,'slot':slot.apply,'visit':visit.apply}

def run_event(con, subsystem, event, batch_id=None, trusted='trusted', now=None):
    if subsystem not in HANDLERS: raise ValueError('unknown-subsystem')
    with con:
        if not storage.record(con,subsystem,event):
            prior=con.execute('SELECT outcome,reason,state FROM decisions WHERE event_id=? ORDER BY id DESC LIMIT 1',(event['id'],)).fetchone()
            return {'event':event['id'],'outcome':'idempotent','prior':dict(prior) if prior else None}
        result=HANDLERS[subsystem](con,event)
        # evidence: source ledger plus per-row projection provenance
        seq=con.execute('SELECT COUNT(*) n FROM events').fetchone()['n']
        row=con.execute('SELECT digest FROM events WHERE id=?',(event['id'],)).fetchone()
        storage.note_event_source(con,event['id'],seq,
                                  int(event.get('effective_at',0)),
                                  int(event.get('received_at',0)),
                                  subsystem,row['digest'],batch_id=batch_id,
                                  trusted=trusted,anchored=batch_id is None,
                                  detail={'captured_at':now} if now is not None else None)
        storage.stamp_event_projections(con,event['id'])
        return result

def audit(con):
    """Legacy-compatible audit, enriched with provenance-grade evidence.

    The returned object keeps ``ok``, ``mismatches`` (table names) and
    ``replayed_events`` so existing callers and tests are unchanged; the
    ``classification`` / ``evidence`` / ``recovery_plans`` keys carry the
    first provable fork, exact divergent rows, causal impact and immutable
    conditional recovery plans.
    """
    return audit_capture.audit_evidence(con)

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['init','demo','report','audit','applicant','slot','visit']); p.add_argument('--db',default='local.db'); p.add_argument('--fixture'); args=p.parse_args(argv)
    con=storage.connect(args.db)
    if args.command=='init': print(json.dumps({'db':args.db,'initialized':True}));return
    if args.command=='report': print(json.dumps(storage.report(con),ensure_ascii=False,indent=2));return
    if args.command=='audit': print(json.dumps(audit(con),ensure_ascii=False,indent=2));return
    if args.command=='demo':
        rows=json.loads((Path(args.fixture) if args.fixture else Path(__file__).resolve().parent.parent/'fixtures'/'demo.json').read_text(encoding='utf-8'))
        result=[run_event(con,x['subsystem'],x['event']) for x in rows]
        print(json.dumps({'results':result,'report':storage.report(con)},ensure_ascii=False,indent=2));return
    if not args.fixture: p.error('--fixture required')
    data=json.loads(Path(args.fixture).read_text(encoding='utf-8'))
    events=data if isinstance(data,list) else [data]
    print(json.dumps([run_event(con,e['subsystem'],e['event']) if 'event' in e else run_event(con,args.command,e) for e in events],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
