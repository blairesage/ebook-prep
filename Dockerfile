# Ebook Prep: clean EPUB/PDF/TXT books into narration-ready text, with a web UI on port 7880.
FROM debian:trixie-slim

# poppler-utils: pdftotext.  ocrmypdf + tesseract: OCR for scanned or badly OCR'd PDFs.
# wamerican/wbritish: word lists for hyphen decisions and the "words to check" list.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip \
      poppler-utils \
      ocrmypdf tesseract-ocr tesseract-ocr-eng \
      wamerican-large wbritish-large \
 && pip3 install --no-cache-dir --break-system-packages pysbd==0.3.4 \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY app/ /app/
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    BOOKS_DIR=/data/books \
    TEXT_DIR=/data/text \
    PORT=7880

EXPOSE 7880
CMD ["python3", "server.py"]
