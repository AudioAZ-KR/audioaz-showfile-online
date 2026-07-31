FROM python:3.11-slim
WORKDIR /app
COPY app/ /app/
RUN pip install --no-cache-dir numbers-parser openpyxl
ENV SHOWFILE_HOST=0.0.0.0
ENV SHOWFILE_ONLINE=1
EXPOSE 8787
CMD ["python", "app_server.py"]
