"""`python -m app.workers.scheduler`: scheduler process (see app/workers/_shell.py)."""

from app.workers._shell import run

if __name__ == "__main__":
    run("scheduler")
