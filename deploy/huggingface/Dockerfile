FROM python:3.12-slim

# Hugging Face Spaces run containers as user 1000
RUN useradd --create-home --uid 1000 user
RUN pip install --no-cache-dir uv

USER user
ENV HOME=/home/user \
    PATH=/home/user/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

# Dependencies first so code changes don't reinstall them
COPY --chown=user pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=user . .

EXPOSE 7860
# --proxy-headers: rate limits see the real client IP behind the Spaces proxy
CMD ["uvicorn", "research_ui_server:app", "--host", "0.0.0.0", "--port", "7860", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
