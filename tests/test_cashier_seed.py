import json
from pathlib import Path
from marrydesk import cashier, settlement

def test_cashier_to_settlement_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'cashier_normal.json').read_text(encoding='utf-8'))
    assert cashier.net_cents(record)==1000
    assert settlement.balance(record)=={'case_id':'COUPLE-1','balance_cents':1000}
