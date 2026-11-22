def paid_minutes(record):
    return sum(item['end_minute']-item['start_minute'] for item in record['shifts'])
