def net_cents(record):
    return sum(item['cents'] for item in record.get('payments', []))-sum(item['cents'] for item in record.get('refunds', []))
