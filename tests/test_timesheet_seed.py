import json
from pathlib import Path
from marrydesk import timesheet, payroll

def test_timesheet_to_payroll_business_flow():
    record=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'timesheet_normal.json').read_text(encoding='utf-8'))
    assert timesheet.paid_minutes(record)==240
    assert payroll.wages(record)=={'staff_id':'CLERK-1','cents':480}
