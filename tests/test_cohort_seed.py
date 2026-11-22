import json
from pathlib import Path
from marrydesk import cohort, access_report

def test_cohort_to_access_report_flow():
    rows=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'cohort_normal.json').read_text(encoding='utf-8'))
    assert cohort.counts(rows)=={'confirmed':14,'requested':20}
    assert access_report.rate(rows)==0.7
