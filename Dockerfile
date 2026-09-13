FROM python:3.12-slim

WORKDIR /app

RUN pip install fastapi uvicorn[standard] jinja2 httpx aiogram segno python-multipart

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
