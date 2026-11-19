# The local demo inside the backend image, so builds run the SCIP indexers in the Linux sandbox
# (README "Local demo"). Test tooling only: adds the dev dependencies from the hash-pinned lock.
# Build from backend/: docker build -t codehover-demo:local -f tests/e2e/demo.Dockerfile .
FROM codehover-cloud-backend:local
USER root
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements-dev.lock && rm /tmp/requirements-dev.lock
USER 10001:10001
