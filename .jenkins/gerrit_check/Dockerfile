# Unit-test image for the Gerrit check (WT-2002), see .jenkins/README.md.
# Same Python as the adapter image (Dockerfile at the repository root).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

# Dependencies first, so the layer is reused until a requirements file changes.
COPY app/requirements.txt app-requirements.txt
COPY tests/requirements.txt tests-requirements.txt
RUN pip install -r app-requirements.txt -r tests-requirements.txt

COPY pyproject.toml ./
COPY app app
COPY tests tests

COPY .jenkins/gerrit_check/run-unit-tests.sh /usr/local/bin/run-unit-tests

CMD ["run-unit-tests"]
