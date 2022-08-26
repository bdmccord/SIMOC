FROM ubuntu:22.04

MAINTAINER Iurii Milovanov "duruku@gmail.com"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3-pip \
    python3-setuptools \
    curl && \
    python3 -m pip install --upgrade pip

COPY ./requirements.txt /simoc/requirements.txt
RUN python3 -m pip install -r /simoc/requirements.txt

COPY . /simoc

ARG DB_TYPE
ARG DB_HOST
ARG DB_PORT
ARG DB_NAME
ARG DB_USER
ARG DB_PASSWORD
ARG REDIS_HOST
ARG REDIS_PORT
ARG REDIS_PASSWORD
ARG FLASK_SECRET

ENV DB_TYPE ${DB_TYPE}
ENV DB_HOST ${DB_HOST}
ENV DB_PORT ${DB_PORT}
ENV DB_NAME ${DB_NAME}
ENV DB_USER ${DB_USER}
ENV DB_PASSWORD ${DB_PASSWORD}
ENV REDIS_HOST ${REDIS_HOST}
ENV REDIS_PORT ${REDIS_PORT}
ENV REDIS_PASSWORD ${REDIS_PASSWORD}
ENV FLASK_SECRET ${FLASK_SECRET}

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

EXPOSE 8080

WORKDIR /simoc

ENTRYPOINT [ "/bin/bash" ]
CMD ["run.sh"]
