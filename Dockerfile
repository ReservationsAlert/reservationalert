FROM python:3.11-slim

WORKDIR /app

# No dependencies to install — pure standard library!
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data

# Use the data directory for persistent storage
ENV DB_PATH=/app/data/reservationalert.db
ENV PORT=8080

EXPOSE 8080

CMD ["python3", "server.py"]
