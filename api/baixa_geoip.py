"""Baixa a base de região por IP (DB-IP Lite, CC BY 4.0) no build da imagem (CS-145).

Tenta o mês atual e o anterior. Se nenhum vier, sai sem erro: a API funciona e
os eventos das coleções ficam sem região.
"""

import datetime
import gzip
import os
import shutil
import sys
import urllib.request

destino = sys.argv[1] if len(sys.argv) > 1 else "/geoip/dbip-city-lite.mmdb"
os.makedirs(os.path.dirname(destino), exist_ok=True)
hoje = datetime.date.today()
anterior = hoje.replace(day=1) - datetime.timedelta(days=1)
for mes in (hoje, anterior):
    url = f"https://download.db-ip.com/free/dbip-city-lite-{mes:%Y-%m}.mmdb.gz"
    try:
        # O servidor recusa (403) o User-Agent padrão do urllib.
        pedido = urllib.request.Request(url, headers={"User-Agent": "mamute-politico-build/1.0"})
        with urllib.request.urlopen(pedido, timeout=180) as resp, open(destino, "wb") as out:
            shutil.copyfileobj(gzip.GzipFile(fileobj=resp), out)
        print("GeoIP:", url)
        break
    except Exception as erro:  # rede fora, mês ainda não publicado
        print("GeoIP indisponível em", url, erro)
        if os.path.exists(destino):
            os.remove(destino)
