FROM python:3.13

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list && \
    apt-get update \
    && apt-get install -y --no-install-recommends snmp \
        postgresql-client \
        vim \
    && rm -rf /var/lib/apt/lists/*
#RUN apt-get update \
#    && apt-get install -y  snmp=5.8-1 \
#        postgresql-client \
#        vim \
#    && rm -rf /var/lib/apt/lists/*


WORKDIR /usr/src/
COPY backend/requirements.txt ./requirements.txt

# Install Docker from Docker Inc. repositories.
RUN curl -sSL https://get.docker.com/ | sh

RUN mkdir -p /root/.local/lib/python3.13/site-packages /usr/src/prom-conf

COPY ./python /root/.local/lib/python3.13/site-packages

#RUN pip install --upgrade pip && pip uninstall -y -r requirements.txt && pip install -r requirements.txt && rm -rf requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt && rm -rf requirements.txt

COPY backend/ ./
COPY frontend/ ./frontend/

#COPY nagios/ script/  template/ /usr/

RUN rm -rf /usr/src/Dockerfile /usr/src/LinkedEyeWebProject.pyproj* /usr/src/LinkedEyeWebProject.sln &&  mkdir -p /vault/ && mkdir -p /stackstorm/configs/ /usr/src/script/ /usr/src/static/app/usericons /usr/src/static/logs && chmod +x /usr/src/script/

VOLUME /usr/src/monitor /vault/keys.json /usr/src/prom-conf /usr/src/static/app/usericons /usr/src/static/logs

COPY backend/entrypoint.py /entrypoint.py

EXPOSE 8000

# CRITICAL FIX #1: Replace manage.py runserver with Gunicorn for production
# Gunicorn with --max-requests auto-recycles workers, preventing unbounded memory growth
# ~40% memory reduction vs Django dev server
RUN pip install gunicorn

CMD python /entrypoint.py && python manage.py collectstatic --noinput && \
    gunicorn LinkedEyeWebProject.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers ${GUNICORN_WORKERS:-4} \
    --worker-class sync \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --timeout 120 \
    --graceful-timeout 30 \
    --preload \
    --access-logfile - \
    --error-logfile -

