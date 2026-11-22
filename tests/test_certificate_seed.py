import json
from pathlib import Path
from marrydesk import certificate, registry

def test_certificate_to_registry_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'certificate_normal.json').read_text(encoding='utf-8'))
    assert certificate.usable(record)
    assert registry.entry(record)=={'case_id':'COUPLE-1','serial':'CERT-100','state':'issued'}
