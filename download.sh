#!/bin/sh

aria2c --enable-rpc --rpc-listen-all=false --rpc-secret="your_secure_token_here"
xvfb-run --auto-servernum uv run main.py "$@"
