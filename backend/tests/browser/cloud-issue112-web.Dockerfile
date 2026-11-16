FROM local/rag-issue112-web-build:test
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", ".next/standalone/server.js"]
