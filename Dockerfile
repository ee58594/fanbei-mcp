# 远程部署用镜像（stdio 客户端不需要它）。
#
#   docker build -t fanbei-mcp .
#   docker run -p 9010:9010 -e FANBEI_BASE=https://trans.sunnytranslation.com fanbei-mcp
#
# 鉴权不在容器里：每个请求自带 Authorization: Bearer <token>，容器不保存任何密钥。
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fanbei_mcp ./fanbei_mcp
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    FANBEI_BASE=https://trans.sunnytranslation.com

EXPOSE 9010
# json-response：不依赖 SSE，前置 Nginx/IIS/Cloudflare 都能正常反代
CMD ["fanbei-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "9010", "--json-response"]
