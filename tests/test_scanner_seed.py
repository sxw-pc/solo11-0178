import json
from pathlib import Path
from marrydesk import scanner, equipment

def test_scanner_to_equipment_business_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'scanner_normal.json').read_text(encoding='utf-8'))
    assert scanner.drift_mm(record)==1
    assert equipment.state(record)=={'device_id':'SCAN-1','state':'可用'}
