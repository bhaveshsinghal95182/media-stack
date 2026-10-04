FROM python:3.13-alpine
WORKDIR /app
COPY core.py web.py watcher.py index.html /app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8099
CMD ["python", "/app/web.py"]
