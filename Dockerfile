# Use official Python slim image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if needed)
RUN apt-get update && apt-get install -y \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create a directory for persistent data (if your bot uses a folder like /data)
RUN mkdir -p /app/data && chmod 755 /app/data

# Declare volume for persistent storage (JSON files)
VOLUME ["/app/data"]

# Expose port (only needed if you use webhooks; for polling it's optional)
EXPOSE 8080

# Start the bot
CMD ["python", "bot.py"]
