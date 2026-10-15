FROM python:3.15-slim

# UID 1000 is usually your user on the host, so it can read your config.toml.
RUN useradd --uid 1000 nanosaas

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY . .

# Sends print() output to docker compose logs right away.
ENV PYTHONUNBUFFERED=1
USER nanosaas
EXPOSE 8000
CMD ["uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "8000"]
