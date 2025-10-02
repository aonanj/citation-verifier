#!/usr/bin/env bash
set -o errexit
set -o pipefail
set -o nounset

# Install system dependencies required for OCR and PDF processing
apt-get update
apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev
rm -rf /var/lib/apt/lists/*

# Install Python dependencies
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt
