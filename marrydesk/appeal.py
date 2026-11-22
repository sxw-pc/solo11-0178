def timely(record):
    return record['filed_at']<=record['deadline']
