import requests
import pandas as pd
import geopandas as gpd

INPE_WFS_URL = "https://terrabrasilis.dpi.inpe.br/queimadas/geoserver/wfs"
INPE_LAYER = "bdqueimadas3:focos_pnt"


def consultar_inpe_wfs(bbox, data_inicio, data_fim, limite=10000):
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": INPE_LAYER,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": f"{bbox},EPSG:4326",
        "count": limite,
    }

    r = requests.get(INPE_WFS_URL, params=params, timeout=120)
    r.raise_for_status()

    data = r.json()

    features = data.get("features", [])

    if not features:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")

    if "data_hora_gmt" in gdf.columns:
        gdf["data_hora_gmt"] = pd.to_datetime(gdf["data_hora_gmt"], errors="coerce", utc=True)

        ini = pd.to_datetime(data_inicio).tz_localize("UTC")
        fim = pd.to_datetime(data_fim).tz_localize("UTC") + pd.Timedelta(days=1)

        gdf = gdf[
            (gdf["data_hora_gmt"] >= ini) &
            (gdf["data_hora_gmt"] < fim)
        ].copy()

    gdf["fonte"] = "BDQueimadas INPE"

    return gdf
