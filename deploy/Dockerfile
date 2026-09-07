FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

ENV SHOP_PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
