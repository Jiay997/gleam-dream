# =============================================================================
# 浮光造梦前端镜像
#
# 静态产物由本地/CI 预构建（frontend/dist），镜像内只做 nginx 托管，
# 避免在低内存服务器上跑 vite 构建。
# =============================================================================

FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY frontend/dist /usr/share/nginx/html

COPY deploy/nginx.conf /etc/nginx/conf.d/app.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
