def drift_mm(record):
    return abs(record['measured_mm']-record['reference_mm'])
