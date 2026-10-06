FROM python:3.11-slim AS base

RUN mkdir /app
WORKDIR /app

#RUN apt update
#RUN apt-get install -f

RUN useradd -m -s /bin/bash httpd
RUN usermod -aG root httpd
RUN mkdir /var/db
RUN chown root:root /var/db
# in Google cloud, the user than runs a container is a semi-random,
# assigned by Google so we need to make the directory writable
RUN chmod 777 /var/db

# add required Python modules
COPY app/requirements.txt /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Unit tests for the Gerrit check (WT-2002): `docker build --target test`, see
# .jenkins/README.md. The tests find the adapter at ../app, so the repository layout is
# kept under /src.
FROM base AS test
WORKDIR /src
COPY tests/requirements.txt tests/
RUN pip install --no-cache-dir -r tests/requirements.txt
COPY pyproject.toml ./
COPY app app
COPY tests tests
# Only test_30 and up are unit tests; test_01..test_15 need a running adapter. One
# pytest per file, because several files stub modules in sys.modules at import and break
# each other in a shared session. Exit 5 is "no tests collected" (test_30 is a plain
# script that checks itself at import), so it counts as a pass.
CMD rc=0; for f in tests/test_[3-9]*.py; do python -m pytest -q -p no:warnings "$f" || [ $? -eq 5 ] || rc=1; done; exit $rc

# The adapter image. Kept last, so a build without --target produces it.
FROM base

COPY app /app/
RUN chmod 755 /app/start-web-server.sh

USER httpd

# the port uvicorn will be listening in the container
ARG API_PORT
ENV API_PORT=${API_PORT:-8080}

ARG BASE_PATH
ENV BASE_PATH=${BASE_PATH:-"/"}

ARG BSS_ADAPTER_MODULE
ENV BSS_ADAPTER_MODULE=${BSS_ADAPTER_MODULE:-"bss.adapters.example"}

ARG BSS_ADAPTER_CLASS
ENV BSS_ADAPTER_CLASS=${BSS_ADAPTER_CLASS:-"ExampleBSSAdapter"}

EXPOSE $API_PORT

CMD ["./start-web-server.sh"]
