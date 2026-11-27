# Build against a local Library image that already contains /opt/docreader-venv.
ARG LIBRARY_BASE_IMAGE=local/library:8091-weknora-figure-r2
FROM ${LIBRARY_BASE_IMAGE}
USER 0
COPY requirements.txt wheels/*.whl /opt/docreader-wheelhouse/
RUN /opt/docreader-venv/bin/python -I -B -m pip --isolated --disable-pip-version-check install --no-cache-dir --no-index --no-deps --require-hashes --find-links /opt/docreader-wheelhouse -r /opt/docreader-wheelhouse/requirements.txt
USER 1001:1001
RUN /opt/docreader-venv/bin/python -I -B -c 'import mammoth, cobble; from importlib.metadata import version; assert version("mammoth") == "1.11.0" and version("cobble") == "0.1.4"'
