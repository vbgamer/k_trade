# Stage 1: Build the React client app
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Build the FastAPI app and copy built client files
FROM python:3.11-slim
WORKDIR /workspace

# Install system dependencies (e.g. build-essential for compiling option indicators if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend files
COPY backend/app/ ./app/

# Copy React bundle from Stage 1 into FastAPI static files folder
COPY --from=frontend-builder /frontend/dist ./app/static/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
