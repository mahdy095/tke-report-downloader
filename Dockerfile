# Hugging Face Spaces — Docker SDK.
# A normal Docker container (root at build time, no seccomp sandbox blocking
# Chromium's child processes) is what makes the headless browser work here,
# where Streamlit Cloud's locked-down container could not.
#
# We install Playwright and then let it pull its OWN version-matched Chromium
# plus every required system library via `playwright install --with-deps`.
# That keeps the browser binary and the pip package perfectly in sync without
# pinning a base-image tag.
FROM python:3.12-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    STREAMLIT_SERVER_PORT=7860 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

COPY requirements-hf.txt ./requirements.txt
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# HF Spaces run as a non-root user with UID 1000.
RUN useradd -m -u 1000 user
COPY --chown=user app.py ./app.py

USER user
EXPOSE 7860

# --server.enableXsrfProtection=false / --enableCORS=false: required for the
# file uploader to work behind Hugging Face's reverse proxy (otherwise the
# upload POST is rejected with "AxiosError: Request failed with status code 403").
CMD ["streamlit", "run", "app.py", \
     "--server.port=7860", "--server.address=0.0.0.0", \
     "--server.enableXsrfProtection=false", "--server.enableCORS=false"]
