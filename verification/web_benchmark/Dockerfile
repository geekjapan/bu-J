ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY fixtures/web_benchmark/fixture.html /opt/fixture/fixture.html
COPY verification/web_benchmark/container.py verification/web_benchmark/proxy.py /opt/web-benchmark/
ENV HOME=/tmp/home TZ=UTC LANG=C.UTF-8
EXPOSE 9223
ENTRYPOINT ["python", "/opt/web-benchmark/container.py"]
CMD []
