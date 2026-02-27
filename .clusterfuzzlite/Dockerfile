FROM gcr.io/oss-fuzz-base/base-builder-python

WORKDIR /src
COPY . /src

# Minimal CFLite integration marker for Scorecard.
RUN python -m compileall /src
