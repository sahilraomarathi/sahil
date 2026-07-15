FROM python:3.10-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create a volume for persistent data (JSON files)
VOLUME ["/app"]

# Run the bot
CMD ["python", "bot.py"]
