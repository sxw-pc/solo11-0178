import json
from pathlib import Path
from marrydesk import appeal, review

def test_appeal_to_review_business_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'appeal_normal.json').read_text(encoding='utf-8'))
    assert appeal.timely(record)
    assert review.intake(record)=={'case_id':'COUPLE-1','state':'受理'}
