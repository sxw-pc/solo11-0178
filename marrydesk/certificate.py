def usable(record):
    return bool(record.get('printed')) and not bool(record.get('voided'))
