FROM node:22-alpine
WORKDIR /app
COPY frontend/.next-cloud-experience/standalone ./
COPY frontend/.next-cloud-experience/static ./.next-cloud-experience/static
COPY frontend/public ./public
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
