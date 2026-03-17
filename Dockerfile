FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG PIP_INDEX_URL=""
ARG PIP_EXTRA_INDEX_URL=""
ARG PDF_PACKAGES="pypdf pdfminer.six PyMuPDF pikepdf pdftotext"
ARG INSTALL_TESSERACT="1"

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}

COPY requirements.txt /app/requirements.txt
RUN if [ "${INSTALL_TESSERACT}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends tesseract-ocr ca-certificates && rm -rf /var/lib/apt/lists/* ; \
      tesseract --version ; \
    else \
      echo "Skipping tesseract install (INSTALL_TESSERACT!=1)"; \
    fi \
  && python -m pip install --upgrade pip \
  && pip install --no-cache-dir -r /app/requirements.txt \
  && mkdir -p /tmp/pip-download \
  && if [ -n "${PIP_INDEX_URL}" ] || [ -n "${PIP_EXTRA_INDEX_URL}" ]; then \
       echo "Checking PDF libs availability via pip mirror..." ; \
       python -m pip download --no-deps --disable-pip-version-check -d /tmp/pip-download ${PDF_PACKAGES} ; \
     else \
       echo "Skipping PDF mirror check (PIP_INDEX_URL/PIP_EXTRA_INDEX_URL not set)"; \
     fi \
  && rm -rf /tmp/pip-download \
  && python -m playwright install --with-deps chromium

COPY smoke_test.py /app/smoke_test.py

CMD ["python", "/app/smoke_test.py"]
