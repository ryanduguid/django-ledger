FROM python:latest

# Copy uv binary from astral-sh
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy dependency files first for caching
COPY pyproject.toml uv.lock /app/

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# Copy the rest of the project
COPY . /app/

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]