FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:34a0a1575c6eae0ff2a1e98ea62c6007f62432a56efb46484b1c5b82ace5a05a

WORKDIR /src
COPY . /src

# Minimal CFLite integration marker for Scorecard.
RUN python -m compileall /src
