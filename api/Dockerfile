FROM python:3.13-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Base de região por IP (DB-IP Lite, CC BY 4.0) para os eventos das coleções
# públicas (CS-145). Camada antes do código: o cache do build reusa.
COPY baixa_geoip.py .
RUN python baixa_geoip.py /geoip/dbip-city-lite.mmdb

COPY db/ db/
COPY routers/ routers/
COPY services/ services/
COPY *.py ./

CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000" ]
