# Gunicorn configuration for SiftCast
# NOTE: workers must stay at 1 because the app runs in-process background threads:
# the periodic feed-refresh scheduler and the AI analysis thread pool. Multiple
# workers would each start their own scheduler/pool and SQLite connection.

workers = 1
worker_class = "sync"
bind = "127.0.0.1:5001"
timeout = 120          # Allow time for RSS fetch + AI analysis kick-off
keepalive = 5
accesslog = "/var/log/siftcast/access.log"
errorlog = "/var/log/siftcast/error.log"
loglevel = "info"
