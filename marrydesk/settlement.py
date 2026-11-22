from . import cashier

def balance(record):
    return {'case_id':record['case_id'],'balance_cents':cashier.net_cents(record)}
