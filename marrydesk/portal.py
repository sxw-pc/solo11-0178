from . import form_fields

def submit(record):
    data=form_fields.fields(record)
    return {'receipt_id':'P-'+data['case_id'],'state':'accepted','submitted':data}
