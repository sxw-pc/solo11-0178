import json
from pathlib import Path
from marrydesk import delivery, service

def test_delivery_to_service_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'delivery_normal.json').read_text(encoding='utf-8'))
    assert delivery.latest_status(record)=='delivered'
    assert service.served(record)=={'case_id':'COUPLE-1','state':'served'}
