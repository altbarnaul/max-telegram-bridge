FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "-m", "max_bridge"]
