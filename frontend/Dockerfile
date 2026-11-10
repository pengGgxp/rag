FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN test -n "$NEXT_PUBLIC_API_URL" \
    && ! printf '%s' "$NEXT_PUBLIC_API_URL" | grep -Eq 'localhost|127\.0\.0\.1' \
    && npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]

