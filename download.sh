#!/bin/sh

xvfb-run --auto-servernum uv run main.py "$@"
