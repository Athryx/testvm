FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        crossbuild-essential-arm64 \
        crossbuild-essential-armhf \
        libncurses-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*
