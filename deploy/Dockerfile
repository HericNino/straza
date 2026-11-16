# Build from the repository root:  docker build -f deploy/Dockerfile -t straza .
# Run:  docker run -p 8080:8080 -v "$PWD/data:/data" straza
#       (put straza.json in ./data; set "web": {"host": "0.0.0.0"} inside it)
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system straza && mkdir /data && chown straza /data
USER straza
WORKDIR /data
EXPOSE 8080
# exec form, so SIGTERM from `docker stop` reaches straza and it shuts down cleanly
ENTRYPOINT ["straza"]
CMD ["run", "-c", "/data/straza.json"]
