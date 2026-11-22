from . import box

def available(record):
    return {'box_id':record['box_id'],'result':'可调阅' if box.location(record)!='out' else '外借中'}
