FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# passlib 1.7.x breaks with bcrypt 5 (every hash/verify raises), and requirements.txt only says ">=4.0.1".
# Constraint added here so the sandbox build is reproducible. Unverified until the first real build.
RUN pip install --no-cache-dir -r requirements.txt "bcrypt>=4.0.1,<5"
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
