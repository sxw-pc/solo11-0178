def latest_status(record):
    scans=record.get('scans', [])
    return max(scans,key=lambda item:item['at'])['status'] if scans else 'untracked'
