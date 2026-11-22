import json
from pathlib import Path
from marrydesk import form_fields, portal

def test_fields_to_portal_business_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'portal_normal.json').read_text(encoding='utf-8'))
    assert form_fields.fields(record)=={'case_id':'CASE-1','party_a':'A','party_b':'B'}
    assert portal.submit(record)=={'receipt_id':'P-CASE-1','state':'accepted','submitted':{'case_id':'CASE-1','party_a':'A','party_b':'B'}}
