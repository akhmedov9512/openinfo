broker_url = 'redis://redis:6379/0'
result_backend = 'redis://redis:6379/0'

task_serializer = 'json'
accept_content = ['json']
result_serializer = 'json'
timezone = 'UTC'
enable_utc = True
task_track_started = True
task_time_limit = 3600

imports = ('api',)