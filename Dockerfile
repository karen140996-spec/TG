FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STATE_DIR=/app/.state
WORKDIR /app
RUN groupadd --gid 10001 publisher && useradd --uid 10001 --gid publisher --no-create-home publisher && mkdir .state && chown publisher:publisher .state
COPY app.py README.md ./
COPY content/week1.json content/week1.json
COPY tests/ tests/
COPY deploy/restore.py deploy/restore.py
RUN chmod -R a+rX /app
USER 10001:10001
RUN python -m unittest discover -s tests -v
CMD ["python", "app.py", "run"]
