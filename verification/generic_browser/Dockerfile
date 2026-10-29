ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY fixtures/generic_browser/ /opt/generic/fixture/
COPY verification/generic_browser/fixture_server.py verification/generic_browser/container.py verification/generic_browser/proxy.py /opt/generic/
COPY verification/web_benchmark/container.py verification/web_benchmark/proxy.py /opt/web_benchmark/
ENV HOME=/tmp/home TZ=UTC LANG=C.UTF-8
EXPOSE 9223
ENTRYPOINT ["python", "/opt/generic/container.py"]
CMD []
