from . import cohort

def rate(rows):
    total=cohort.counts(rows)
    return total['confirmed']/total['requested'] if total['requested'] else None

def summary(rows):
    total=cohort.counts(rows)
    return {'confirmed':total['confirmed'],'requested':total['requested'],
            'rate':total['confirmed']/total['requested'] if total['requested'] else None}
