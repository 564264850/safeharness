FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

COPY requirements.txt /opt/safeharness/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --index-url https://pypi.org/simple -r /opt/safeharness/requirements.txt
COPY *.py /opt/safeharness/
COPY pytest.ini /opt/safeharness/pytest.ini
COPY verifier/ /opt/safeharness/verifier/
ARG SOURCE_SHA256
LABEL safeharness.source-sha256=$SOURCE_SHA256
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 65534:65534
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/safeharness/probe.py"]
