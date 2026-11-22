import json
from pathlib import Path
from marrydesk import box, loan

def test_box_to_loan_business_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'box_normal.json').read_text(encoding='utf-8'))
    assert box.location(record)=='S-4'
    assert loan.available(record)=={'box_id':'BOX-1','result':'可调阅'}
