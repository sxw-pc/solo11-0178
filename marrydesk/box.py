def location(record):
    return record['shelf'] if not record.get('checked_out') else 'out'
