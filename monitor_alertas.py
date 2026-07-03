import os
import io
import sqlite3
import smtplib
import requests
import pandas as pd
import geopandas as gpd

from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from inpe_wfs import consultar_inpe_wfs

AREA_PATH = os.getenv("AREA_GEOJSON_PATH", "data/area_monitorada.geojson")
DB_PATH = os.getenv("ALERT_DB_PATH", "data/alert_history.db")

BUFFER_KM = float(os.getenv("BUFFER_KM", "10"))
FIRMS_DAY_RANGE = 1

FIRMS_SOURCES = [
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT"
]

FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY")

ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id TEXT PRIMARY KEY,
            data_alerta TEXT,
            fonte TEXT,
            data_foco TEXT,
            hora_foco TEXT,
            sensor TEXT,
            latitude REAL,
            longitude REAL,
            frp REAL,
            classe_espacial TEXT,
            distancia_borda_km REAL,
            status_alerta TEXT
        )
    """)

    conn.commit()
    conn.close()


def carregar_area():
    if not os.path.exists(AREA_PATH):
        raise FileNotFoundError(f"Arquivo da área não encontrado: {AREA_PATH}")

    gdf = gpd.read_file(AREA_PATH)

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")

    return gdf.to_crs("EPSG:4326")


def gerar_buffer(gdf_area):
    area_m = gdf_area.to_crs("EPSG:5880")
    area_union = area_m.geometry.union_all()

    buffer_geom = area_union.buffer(BUFFER_KM * 1000).difference(area_union)

    return gpd.GeoDataFrame(
        geometry=[buffer_geom],
        crs="EPSG:5880"
    ).to_crs("EPSG:4326")


def bbox_total(gdf_area, gdf_buffer):
    gdf_all = pd.concat([gdf_area, gdf_buffer], ignore_index=True)
    minx, miny, maxx, maxy = gdf_all.total_bounds
    return f"{minx},{miny},{maxx},{maxy}"


def consultar_firms(bbox):
    if not FIRMS_MAP_KEY:
        print("FIRMS_MAP_KEY não configurada. Pulando NASA FIRMS.")
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    data_consulta = date.today() - timedelta(days=1)
    dfs = []

    for sensor in FIRMS_SOURCES:
        url = (
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{FIRMS_MAP_KEY}/{sensor}/{bbox}/{FIRMS_DAY_RANGE}/{data_consulta.isoformat()}"
        )

        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()

            if not r.text.strip():
                continue

            df = pd.read_csv(io.StringIO(r.text))

            if df.empty or "latitude" not in df.columns or "longitude" not in df.columns:
                continue

            df["sensor"] = sensor
            df["fonte"] = "NASA FIRMS"
            df["data_foco"] = df.get("acq_date", "")
            df["hora_foco"] = df.get("acq_time", "")
            dfs.append(df)

        except Exception as e:
            print(f"Falha FIRMS {sensor}: {e}")

    if not dfs:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    df_all = pd.concat(dfs, ignore_index=True).drop_duplicates()

    return gpd.GeoDataFrame(
        df_all,
        geometry=gpd.points_from_xy(df_all["longitude"], df_all["latitude"]),
        crs="EPSG:4326"
    )


def normalizar_inpe(gdf):
    if gdf.empty:
        return gdf

    gdf = gdf.copy()

    if "latitude" not in gdf.columns:
        gdf["latitude"] = gdf.geometry.y

    if "longitude" not in gdf.columns:
        gdf["longitude"] = gdf.geometry.x

    if "data_hora_gmt" in gdf.columns:
        dt = pd.to_datetime(gdf["data_hora_gmt"], errors="coerce", utc=True)
        gdf["data_foco"] = dt.dt.strftime("%Y-%m-%d")
        gdf["hora_foco"] = dt.dt.strftime("%H:%M")
    else:
        gdf["data_foco"] = ""
        gdf["hora_foco"] = ""

    if "satelite" in gdf.columns:
        gdf["sensor"] = gdf["satelite"]
    elif "sensor" not in gdf.columns:
        gdf["sensor"] = "INPE"

    gdf["fonte"] = "BDQueimadas INPE"

    return gdf


def classificar_focos(gdf_focos, gdf_area, gdf_buffer):
    if gdf_focos.empty:
        return gdf_focos

    area_union_geo = gdf_area.geometry.union_all()
    buffer_union_geo = gdf_buffer.geometry.union_all()
    area_union_m = gdf_area.to_crs("EPSG:5880").geometry.union_all()

    gdf = gdf_focos.to_crs("EPSG:4326").copy()
    gdf_m = gdf.to_crs("EPSG:5880")

    classes = []
    distancias = []
    status = []

    for idx, row in gdf.iterrows():
        geom = row.geometry

        if geom.within(area_union_geo):
            classes.append("Dentro da TI")
            distancias.append(0.0)
            status.append("CRITICO")

        elif geom.within(buffer_union_geo):
            geom_m = gdf_m.loc[idx].geometry
            dist_km = geom_m.distance(area_union_m) / 1000
            classes.append(f"Entorno {BUFFER_KM:g} km")
            distancias.append(round(dist_km, 3))
            status.append("PREVENTIVO")

        else:
            classes.append("Fora")
            distancias.append(None)
            status.append("SEM_ALERTA")

    gdf["classe_espacial"] = classes
    gdf["distancia_borda_km"] = distancias
    gdf["status_alerta"] = status

    return gdf[gdf["status_alerta"].isin(["CRITICO", "PREVENTIVO"])].copy()


def foco_id(row):
    fonte = str(row.get("fonte", ""))
    sensor = str(row.get("sensor", ""))
    data = str(row.get("data_foco", ""))
    hora = str(row.get("hora_foco", ""))

    if "id_foco_bdq" in row and pd.notna(row.get("id_foco_bdq")):
        return f"INPE_{row.get('id_foco_bdq')}"

    lat = round(float(row["latitude"]), 4)
    lon = round(float(row["longitude"]), 4)

    return f"{fonte}_{sensor}_{data}_{hora}_{lat}_{lon}"


def ja_alertado(id_foco):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM alert_history WHERE id = ?", (id_foco,))
    existe = cur.fetchone() is not None
    conn.close()
    return existe


def salvar_alerta(row):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO alert_history (
            id,
            data_alerta,
            fonte,
            data_foco,
            hora_foco,
            sensor,
            latitude,
            longitude,
            frp,
            classe_espacial,
            distancia_borda_km,
            status_alerta
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["id_foco"],
        datetime.utcnow().isoformat(),
        str(row.get("fonte", "")),
        str(row.get("data_foco", "")),
        str(row.get("hora_foco", "")),
        str(row.get("sensor", "")),
        float(row.get("latitude", 0)),
        float(row.get("longitude", 0)),
        float(row.get("frp", 0)) if pd.notna(row.get("frp", None)) else 0,
        str(row.get("classe_espacial", "")),
        float(row.get("distancia_borda_km", 0)) if pd.notna(row.get("distancia_borda_km", None)) else 0,
        str(row.get("status_alerta", ""))
    ))

    conn.commit()
    conn.close()


def focos_novos(gdf_alertas):
    if gdf_alertas.empty:
        return gdf_alertas

    gdf = gdf_alertas.copy()
    gdf["id_foco"] = gdf.apply(foco_id, axis=1)

    novos = []

    for _, row in gdf.iterrows():
        if not ja_alertado(row["id_foco"]):
            novos.append(row)

    if not novos:
        return gpd.GeoDataFrame(columns=gdf.columns, geometry="geometry", crs=gdf.crs)

    return gpd.GeoDataFrame(novos, geometry="geometry", crs=gdf.crs)


def montar_mensagem(gdf_novos):
    criticos = gdf_novos[gdf_novos["status_alerta"] == "CRITICO"]
    preventivos = gdf_novos[gdf_novos["status_alerta"] == "PREVENTIVO"]

    linhas = [
        "🔥 ALERTA KW'Y GEOMONITOR",
        "",
        f"Novos focos detectados: {len(gdf_novos)}",
        f"Críticos dentro da TI: {len(criticos)}",
        f"Preventivos no entorno de {BUFFER_KM:g} km: {len(preventivos)}",
        "",
        "Resumo dos focos:"
    ]

    for _, row in gdf_novos.head(20).iterrows():
        lat = row.get("latitude", "")
        lon = row.get("longitude", "")
        dist = row.get("distancia_borda_km", "")

        nivel = "🔴 CRÍTICO" if row.get("status_alerta") == "CRITICO" else "🟡 PREVENTIVO"
        dist_txt = "dentro da TI" if row.get("status_alerta") == "CRITICO" else f"{dist} km da borda da TI"

        linhas.append("")
        linhas.append(nivel)
        linhas.append(f"- Fonte: {row.get('fonte', '')}")
        linhas.append(f"- Classe: {row.get('classe_espacial', '')}")
        linhas.append(f"- Data/hora: {row.get('data_foco', '')} {row.get('hora_foco', '')}")
        linhas.append(f"- Sensor/Satélite: {row.get('sensor', '')}")
        linhas.append(f"- FRP: {row.get('frp', '')}")
        linhas.append(f"- Coordenadas: {lat}, {lon}")
        linhas.append(f"- Distância: {dist_txt}")
        linhas.append(f"- Mapa: https://www.google.com/maps?q={lat},{lon}")

        if row.get("fonte") == "BDQueimadas INPE":
            linhas.append(f"- Risco de fogo INPE: {row.get('risco_fogo', '')}")
            linhas.append(f"- Dias sem chuva: {row.get('numero_dias_sem_chuva', '')}")
            linhas.append(f"- Precipitação: {row.get('precipitacao', '')}")
            linhas.append(f"- Município/UF: {row.get('municipio', '')}/{row.get('estado', '')}")
            linhas.append(f"- Bioma: {row.get('bioma', '')}")

    linhas.append("")
    linhas.append("Recomenda-se verificar a ocorrência e acionar o protocolo de monitoramento territorial.")

    return "\n".join(linhas)


def enviar_email(mensagem, assunto):
    if not all([ALERT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD]):
        print("E-mail não configurado. Pulando envio.")
        return

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO
    msg["Subject"] = assunto
    msg.attach(MIMEText(mensagem, "plain", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print("E-mail enviado.")


def main():
    print("===================================================")
    print("KW'Y GEOMONITOR - Verificação automática")
    print(f"Data UTC: {datetime.utcnow().isoformat()}")
    print(f"Buffer configurado: {BUFFER_KM:g} km")
    print("===================================================")

    init_db()

    data_inicio = date.today() - timedelta(days=1)
    data_fim = date.today()

    gdf_area = carregar_area()
    gdf_buffer = gerar_buffer(gdf_area)
    bbox = bbox_total(gdf_area, gdf_buffer)

    print(f"BBOX consultado: {bbox}")

    gdf_firms = consultar_firms(bbox)
    print(f"Focos brutos NASA FIRMS: {len(gdf_firms)}")

    try:
        gdf_inpe = consultar_inpe_wfs(bbox, data_inicio, data_fim)
        gdf_inpe = normalizar_inpe(gdf_inpe)
        print(f"Focos brutos BDQueimadas INPE: {len(gdf_inpe)}")
    except Exception as e:
        print(f"Falha ao consultar BDQueimadas INPE: {e}")
        gdf_inpe = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    gdf_todos = pd.concat([gdf_firms, gdf_inpe], ignore_index=True)

    if gdf_todos.empty:
        print("Nenhum foco encontrado nas fontes consultadas.")
        return

    gdf_todos = gpd.GeoDataFrame(gdf_todos, geometry="geometry", crs="EPSG:4326")

    gdf_alertas = classificar_focos(gdf_todos, gdf_area, gdf_buffer)
    print(f"Focos dentro da TI ou buffer: {len(gdf_alertas)}")

    if gdf_alertas.empty:
        print("Nenhum foco detectado dentro da TI ou no buffer.")
        return

    gdf_novos = focos_novos(gdf_alertas)
    print(f"Focos novos ainda não alertados: {len(gdf_novos)}")

    if gdf_novos.empty:
        print("Todos os focos já foram alertados anteriormente.")
        return

    mensagem = montar_mensagem(gdf_novos)

    criticos = int((gdf_novos["status_alerta"] == "CRITICO").sum())
    preventivos = int((gdf_novos["status_alerta"] == "PREVENTIVO").sum())

    if criticos > 0:
        assunto = f"🔴 ALERTA CRÍTICO: {criticos} foco(s) dentro da TI"
    else:
        assunto = f"🟡 ALERTA PREVENTIVO: {preventivos} foco(s) no entorno {BUFFER_KM:g} km"

    print(mensagem)

    enviar_email(mensagem, assunto)

    for _, row in gdf_novos.iterrows():
        salvar_alerta(row)

    print("Histórico de alertas atualizado.")


if __name__ == "__main__":
    main()
