"""
Gunicorn configuration for CodeSage.

CodeSage streams long-lived Server-Sent Events and keeps its run registry
(used by Stop/cancel) in process memory, so it runs as ONE process with many
threads: every request — including /api/chat/cancel — reaches the process that
owns the run, and a slow LLM stream never blocks other users. LLM calls are
I/O-bound, so threads scale well here. To scale out, run more instances behind
a load balancer with sticky sessions.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "32"))
# Worker heartbeat timeout (not a request limit — LLM calls have their own deadlines)
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 75
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
forwarded_allow_ips = "*"
