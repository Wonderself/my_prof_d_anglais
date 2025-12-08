# Use the official Python image as a base
FROM python:3.11-slim

# Install system dependencies (FFmpeg is mandatory for pydub audio processing)
RUN apt-get update && apt-get install -y ffmpeg

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Define the start command for Gunicorn
# Render defaults to port 10000
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]