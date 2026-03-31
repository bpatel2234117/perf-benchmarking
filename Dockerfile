FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (useful for building Python packages like pandas if wheels are missing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependencies list
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all other project files into the container
COPY . .

# Expose the default Streamlit port
EXPOSE 8501

# Default command matches the Streamlit dashboard
CMD ["streamlit", "run", "dashboard.py", "--server.port=8501", "--server.address=0.0.0.0"]
