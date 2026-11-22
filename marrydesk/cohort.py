def counts(rows):
    return {'confirmed':sum(row['confirmed'] for row in rows),
            'requested':sum(row['requested'] for row in rows)}
