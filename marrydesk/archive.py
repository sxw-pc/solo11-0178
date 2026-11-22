def complete(record):
    return all(kind in record.get('documents', []) for kind in record['required'])
