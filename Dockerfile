# Base image
FROM python:3.12-alpine

# Install gcc and other necessary build tools
RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev

EXPOSE 5000

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG BUILD_SHA=dev
ENV BUILD_SHA=${BUILD_SHA}

# Create config dir for persistent settings
RUN mkdir -p /config

# Install pip requirements
COPY requirements.txt .
RUN python -m pip install -r requirements.txt

WORKDIR /app
COPY . /app

VOLUME ["/downloads", "/config"]

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--workers=1", "--worker-class=gevent", "qbdl_gui:app"]
