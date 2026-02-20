# YouTube Monitoring Add-on
FROM alpine:3.19

RUN apk add --no-cache python3 py3-requests ca-certificates tzdata

WORKDIR /app

COPY app /app/app
COPY main.py /app/main.py

CMD ["python3", "/app/main.py"]
