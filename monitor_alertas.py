import os
import io
import smtplib
import requests
import pandas as pd
import geopandas as gpd
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

FIRMS_DAY_RANGE = 1
FIRMS_SOURCES = [
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT"
]

AREA_PATH = os.getenv("AREA_GEOJSON_PATH", "data/area_monitorada.geojson")
FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY")

ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def carregar_area():
    if not os.path.exists(AREA_PATH):
        raise FileNotFoundError(f"Arquivo da área não encontrado: {AREA_PATH}")

    gdf = gpd.read_file(AREA_PATH)

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")

    return gdf.to_crs("EPSG:4326")


def bbox_area(gdf):
    minx, miny, maxx, maxy = gdf.total_bounds
    return f"{minx},{miny},{maxx},{maxy}"


def consultar_firms(bbox):
    if not FIRMS_MAP_KEY:
        raise ValueError("FIRMS_MAP_KEY não configurada.")

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


def filtrar_dentro_area(gdf_focos, gdf_area):
    if gdf_focos.empty:
        return gdf_focos

    area_union = gdf_area.geometry.union_all()

    gdf = gdf_focos.copy()
    gdf["dentro_area"] = gdf.geometry.apply(lambda geom: geom.within(area_union))

    return gdf[gdf["dentro_area"]].copy()


def montar_mensagem(gdf_alerta):
    total = len(gdf_alerta)

    linhas = [
        "🔥 ALERTA KW'Y GEOMONITOR",
        "",
        f"Foram detectados {total} foco(s) de queimada dentro da área monitorada.",
        "",
        "Resumo dos focos:"
    ]

    for _, row in gdf_alerta.head(10).iterrows():
        data = row.get("acq_date", "sem data")
        hora = row.get("acq_time", "sem hora")
        sensor = row.get("sensor", "sensor não informado")
        lat = row.get("latitude", "")
        lon = row.get("longitude", "")
        frp = row.get("frp", "sem FRP")

        linhas.append(
            f"- {data} {hora} | {sensor} | lat {lat} lon {lon} | FRP {frp}"
        )

    linhas.append("")
    linhas.append("Recomenda-se verificar a ocorrência e acionar o protocolo de monitoramento territorial.")

    return "\n".join(linhas)


def enviar_email(mensagem):
    if not all([ALERT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD]):
        print("E-mail não configurado. Pulando envio.")
        return

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO
    msg["Subject"] = "🔥 Alerta de Queimada - Kw'y GeoMonitor"

    msg.attach(MIMEText(mensagem, "plain", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print("E-mail enviado.")


def enviar_telegram(mensagem):
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("Telegram não configurado. Pulando envio.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem
    }

    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()

    print("Telegram enviado.")


def main():
    print("Iniciando verificação automática de queimadas...")

    gdf_area = carregar_area()
    bbox = bbox_area(gdf_area)

    gdf_focos = consultar_firms(bbox)
    gdf_alerta = filtrar_dentro_area(gdf_focos, gdf_area)

    if gdf_alerta.empty:
        print("Nenhum foco detectado dentro da área.")
        return

    mensagem = montar_mensagem(gdf_alerta)

    print(mensagem)

    enviar_email(mensagem)
    enviar_telegram(mensagem)


if __name__ == "__main__":
    main()
