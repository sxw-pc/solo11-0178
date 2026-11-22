from . import appeal

def intake(record):
    return {'case_id':record['case_id'],'state':'受理' if appeal.timely(record) else '逾期'}
