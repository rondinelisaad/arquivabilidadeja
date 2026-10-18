FROM mcr.microsoft.com/playwright:v1.64.0-noble@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f

ENV NODE_ENV=production \
    HOME=/tmp/browser-home

WORKDIR /opt/arquivabilidade-browser
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force
COPY server.mjs security.mjs ./

USER pwuser
EXPOSE 9223
CMD ["node", "server.mjs"]
