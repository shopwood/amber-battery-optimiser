ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip tzdata

COPY src/ /app/
RUN pip3 install --no-cache-dir --break-system-packages \
    httpx==0.27.* pydantic==2.* apscheduler==3.10.*

COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
