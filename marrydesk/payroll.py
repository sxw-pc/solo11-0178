from . import timesheet

def wages(record):
    return {'staff_id':record['staff_id'],'cents':timesheet.paid_minutes(record)*record['cents_per_minute']}
