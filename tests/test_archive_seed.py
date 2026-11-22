import json
from pathlib import Path
from marrydesk import archive, retention

def test_archive_to_retention_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'archive_normal.json').read_text(encoding='utf-8'))
    assert archive.complete(record)
    assert retention.disposition(record)=={'case_id':'COUPLE-1','state':'ready'}
