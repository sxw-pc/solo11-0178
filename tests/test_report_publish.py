import json, sqlite3
from pathlib import Path
import pytest
from marrydesk import report_capture, report_publish, cohort, access_report

FIXTURE=Path(__file__).resolve().parents[1]/'fixtures'/'report-publish.json'
DATA=json.loads(FIXTURE.read_text(encoding='utf-8'))
MONTH=DATA['month']
V1=DATA['expect']['v1']; V2=DATA['expect']['v2']

def connect(tmp_path):
    con=sqlite3.connect(tmp_path/'report.db'); con.row_factory=sqlite3.Row
    return con

def ingest_v1(con):
    for batch in DATA['batches']: report_capture.ingest(con,{**batch,'month':MONTH})

def ingest_correction(con):
    report_capture.ingest(con,{**DATA['correction'],'month':MONTH})

def csv_rows(text):
    rows=[]
    for line in text.strip().splitlines()[1:]:
        window,requested,confirmed=line.split(',')
        rows.append({'window':window,'requested':int(requested),'confirmed':int(confirmed)})
    return rows

def test_cohort_caliber_and_v1_publish(tmp_path):
    rows=[row for batch in DATA['batches'] for row in batch['rows']]
    assert cohort.counts(rows)=={'confirmed':V1['confirmed'],'requested':V1['requested']}
    assert access_report.rate(rows)==V1['rate']==0.7
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con)
    result=report_publish.publish(con,store,MONTH,now=10)
    assert (result['outcome'],result['version'])==('committed',1)
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==1 and view['rate']==V1['rate']
    assert (view['confirmed'],view['requested'])==(V1['confirmed'],V1['requested'])
    assert access_report.rate(csv_rows(view['csv']))==V1['rate']
    assert view['json']['rate']==V1['rate'] and view['json']['version']==1
    con.close()

def test_capture_records_raw_bytes_arrival_and_revocation(tmp_path):
    con=connect(tmp_path)
    first={**DATA['batches'][0],'month':MONTH}
    got=report_capture.ingest(con,first)
    assert got['outcome']=='captured'
    again=report_capture.ingest(con,first)
    assert again['outcome']=='idempotent' and again['digest']==got['digest']
    stored=report_capture.get(con,first['id'])
    assert stored['window']=='L' and stored['revision']==1 and stored['arrived_at']==first['arrived_at']
    assert stored['digest']==report_capture.digest_of(stored['raw'])
    assert json.loads(stored['raw'])['rows']==first['rows']
    with pytest.raises(ValueError):
        report_capture.ingest(con,{**first,'rows':[{'office':'L','requested':9,'confirmed':9}]})
    assert report_capture.revoke(con,first['id'],revoked_at=99,reason='dup')['outcome']=='revoked'
    assert report_capture.revoke(con,first['id'],revoked_at=100)['outcome']=='idempotent'
    assert report_capture.get(con,first['id'])['revoked']==1
    assert [e['kind'] for e in report_capture.events(con)]==['ingest','revoke']
    con.close()

def test_effective_uses_latest_non_revoked_revision(tmp_path):
    con=connect(tmp_path)
    ingest_v1(con); ingest_correction(con)
    assert [(b['window'],b['revision']) for b in report_capture.effective(con,MONTH)]==[('C',2),('L',1)]
    report_capture.revoke(con,DATA['correction']['id'],revoked_at=DATA['revoke']['revoked_at'])
    assert [(b['window'],b['revision']) for b in report_capture.effective(con,MONTH)]==[('C',1),('L',1)]
    con.close()

def test_late_correction_revises_v1_to_v2_and_v1_stays_readable(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con)
    assert report_publish.publish(con,store,MONTH,now=10)['version']==1
    ingest_correction(con)
    result=report_publish.publish(con,store,MONTH,now=20)
    assert (result['outcome'],result['version'])==('committed',2)
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==2 and view['rate']==V2['rate']==0.6
    assert (view['confirmed'],view['requested'])==(V2['confirmed'],V2['requested'])
    assert access_report.rate(csv_rows(view['csv']))==V2['rate']
    assert [s['id'] for s in view['json']['sources']]==['daily-C-r2','daily-L-r1']
    old=report_publish.read_version(con,store,MONTH,1)
    assert old['rate']==V1['rate'] and access_report.rate(csv_rows(old['csv']))==V1['rate']
    assert report_publish.current_version(con,MONTH)==2
    con.close()

def test_staged_v2_csv_is_invisible_until_pointer_flips(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con); report_publish.publish(con,store,MONTH,now=10)
    ingest_correction(con)
    with pytest.raises(report_publish.PowerFailure):
        report_publish.publish(con,store,MONTH,now=20,_crash='after_csv_stage')
    staged=(store/'staging'/MONTH/'v2.csv').read_text(encoding='utf-8')
    assert access_report.rate(csv_rows(staged))==V2['rate']
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==1 and view['rate']==V1['rate']
    assert access_report.rate(csv_rows(view['csv']))==V1['rate']
    assert view['json']['rate']==V1['rate']
    con.close()

@pytest.mark.parametrize('hook',['after_csv_stage','after_stage','after_csv_commit','after_json_commit'])
def test_crash_mid_publish_then_recovery_completes_v2(tmp_path,hook):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con); report_publish.publish(con,store,MONTH,now=10)
    ingest_correction(con)
    with pytest.raises(report_publish.PowerFailure):
        report_publish.publish(con,store,MONTH,now=20,_crash=hook)
    con.close()
    con=connect(tmp_path)
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==1 and view['rate']==V1['rate']
    assert access_report.rate(csv_rows(view['csv']))==V1['rate']
    outcomes=report_publish.recover(con,store,MONTH,now=30)
    assert outcomes==[{'outcome':'completed','version':2}]
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==2 and view['rate']==V2['rate']
    assert access_report.rate(csv_rows(view['csv']))==V2['rate']
    assert view['json']['rate']==V2['rate'] and view['json']['version']==2
    assert report_publish.read_version(con,store,MONTH,1)['rate']==V1['rate']
    con.close()

def test_crash_then_revoked_batch_rolls_back_to_v1(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con); report_publish.publish(con,store,MONTH,now=10)
    ingest_correction(con)
    with pytest.raises(report_publish.PowerFailure):
        report_publish.publish(con,store,MONTH,now=20,_crash='after_csv_stage')
    torn=store/'staging'/MONTH/'v2.json'
    torn.parent.mkdir(parents=True,exist_ok=True)
    torn.write_text('{"month":',encoding='utf-8')
    report_capture.revoke(con,DATA['revoke']['id'],revoked_at=DATA['revoke']['revoked_at'],reason=DATA['revoke']['reason'])
    con.close()
    con=connect(tmp_path)
    outcomes=report_publish.recover(con,store,MONTH,now=30)
    assert outcomes[0]['outcome']=='rolled-back'
    assert DATA['correction']['id'] in outcomes[0]['diagnostic']
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==1 and view['rate']==V1['rate']
    assert access_report.rate(csv_rows(view['csv']))==V1['rate']
    versions={v['version']:v for v in report_publish.versions(con,MONTH)}
    assert versions[2]['state']=='quarantined' and versions[2]['diagnostic']
    qdir=store/'quarantine'/MONTH/'v2'
    assert (qdir/'v2.csv').exists() and (qdir/'v2.json').exists()
    assert not (store/'versions'/MONTH/'v2.csv').exists()
    assert report_publish.current_version(con,MONTH)==1
    con.close()

def test_crash_after_pointer_update_keeps_v1_readable_and_counts_v2_once(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con); report_publish.publish(con,store,MONTH,now=10)
    ingest_correction(con)
    with pytest.raises(report_publish.PowerFailure):
        report_publish.publish(con,store,MONTH,now=20,_crash='after_pointer')
    con.close()
    con=connect(tmp_path)
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==2 and view['rate']==V2['rate']
    assert report_publish.read_version(con,store,MONTH,1)['rate']==V1['rate']
    assert report_publish.recover(con,store,MONTH,now=30)==[]
    again=report_publish.publish(con,store,MONTH,now=40)
    assert again['outcome']=='idempotent' and again['version']==2
    committed=[v['version'] for v in report_publish.versions(con,MONTH) if v['state']=='committed']
    assert committed==[1,2]
    assert report_publish.current_version(con,MONTH)==2
    con.close()

def test_rerun_same_input_never_overwrites_committed_artifacts(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con)
    assert report_publish.publish(con,store,MONTH,now=10)['outcome']=='committed'
    before={p.name:p.read_bytes() for p in (store/'versions'/MONTH).iterdir()}
    again=report_publish.publish(con,store,MONTH,now=11)
    assert again['outcome']=='idempotent' and again['version']==1
    after={p.name:p.read_bytes() for p in (store/'versions'/MONTH).iterdir()}
    assert before==after
    assert len(report_publish.versions(con,MONTH))==1
    con.close()

def test_invalid_input_never_advances_current_pointer(tmp_path):
    con=connect(tmp_path); store=tmp_path/'store'
    ingest_v1(con); report_publish.publish(con,store,MONTH,now=10)
    bad={'id':'daily-C-r9','window':'C','revision':9,'arrived_at':20261109,
         'rows':[{'office':'C','group':'A','requested':10,'confirmed':15}]}
    report_capture.ingest(con,{**bad,'month':MONTH})
    result=report_publish.publish(con,store,MONTH,now=20)
    assert result['outcome']=='invalid' and 'confirmed' in result['diagnostic']
    view=report_publish.read_current(con,store,MONTH)
    assert view['version']==1 and view['rate']==V1['rate']
    versions={v['version']:v for v in report_publish.versions(con,MONTH)}
    assert versions[2]['state']=='failed'
    report_capture.revoke(con,bad['id'],revoked_at=20261110)
    again=report_publish.publish(con,store,MONTH,now=40)
    assert again['outcome']=='idempotent' and again['version']==1
    assert report_publish.current_version(con,MONTH)==1
    con.close()
