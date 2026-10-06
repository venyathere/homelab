FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1

RUN mkdir /data && chown 65534:65534 /data
COPY sync.py /app/sync.py

USER 65534:65534
VOLUME /data
CMD ["python", "/app/sync.py"]

