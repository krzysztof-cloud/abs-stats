FROM python:3.12-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
EXPOSE 8484
CMD ["python", "-u", "/app/app.py"]
