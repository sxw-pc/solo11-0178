from . import cohort

def rate(rows):
    total=cohort.counts(rows)
    return total['confirmed']/total['requested'] if total['requested'] else None
