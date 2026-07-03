import os, io, zipfile, tempfile
from datetime import date, timedelta

import requests
import pandas as pd
import geopandas as gpd
import streamlit as st
import folium
import matplotlib.pyplot as plt

from streamlit_folium import st_folium
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors


st.set_page_config(page_title="Kw'y GeoMonitor", page_icon="🔥", layout="wide")

FIRMS_SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]
FIRMS_DAY_RANGE = 5
POWER_PARAM = "PRECTOTCORR"
CRS_GEO = "EPSG:4326"
CRS_METRICO = "EPSG:5880"


def init_state():
    defaults = {
        "area_loaded": False,
        "gdf_area": None,
        "gdf_buffer": None,
        "area_info": None,
        "buffer_km": 10,
        "area_name": "TI Xikrin do Cateté",
        "df_chuva": None,
        "gdf_firms": None,
        "gdf_inpe": None,
        "periodo": None,
        "pdf_bytes": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def load_area(uploaded_file):
    name = uploaded_file.name.lower()

    if name.endswith((".geojson", ".json")):
        gdf = gpd.read_file(uploaded_file)

    elif name.endswith(".zip"):
        temp_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(uploaded_file, "r") as z:
            z.extractall(temp_dir)

        shp_files = []
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.lower().endswith(".shp"):
                    shp_files.append(os.path.join(root, f))

        if not shp_files:
            raise ValueError("Nenhum arquivo .shp encontrado dentro do ZIP.")

        gdf = gpd.read_file(shp_files[0])

    else:
        raise ValueError("Formato não suportado. Envie GeoJSON ou Shapefile ZIP.")

    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_GEO)

    return gdf.to_crs(CRS_GEO)


def get_area_info(gdf):
    gdf_m = gdf.to_crs(CRS_METRICO)
    minx, miny, maxx, maxy = gdf.total_bounds
    centroid = gdf.geometry.union_all().centroid

    return {
        "area_ha": gdf_m.geometry.area.sum() / 10000,
        "perimetro_km": gdf_m.geometry.length.sum() / 1000,
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "centroid_lat": centroid.y,
        "centroid_lon": centroid.x,
        "poligonos": len(gdf)
    }


def create_buffer(gdf, buffer_km):
    gdf_m = gdf.to_crs(CRS_METRICO)
    geom = gdf_m.geometry.union_all()
    buffer_geom = geom.buffer(buffer_km * 1000).difference(geom)
    return gpd.GeoDataFrame(geometry=[buffer_geom], crs=CRS_METRICO).to_crs(CRS_GEO)


def classify_points(gdf_points, gdf_area, gdf_buffer, buffer_km):
    if gdf_points is None or gdf_points.empty:
        return gdf_points

    area_union = gdf_area.geometry.union_all()
    buffer_union = gdf_buffer.geometry.union_all() if gdf_buffer is not None else None

    gdf = gdf_points.to_crs(CRS_GEO).copy()

    def cls(geom):
        if geom.within(area_union):
            return "Dentro do território"
        if buffer_union is not None and geom.within(buffer_union):
            return f"Entorno {buffer_km} km"
        return "Fora"

    gdf["classe_espacial"] = gdf.geometry.apply(cls)

    return gdf[gdf["classe_espacial"].isin(["Dentro do território", f"Entorno {buffer_km} km"])].copy()


def get_chuva(lat, lon, start_date, end_date):
    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = {
        "parameters": POWER_PARAM,
        "community": "AG",
        "longitude": lon,
        "latitude": lat,
        "start": start_date.strftime("%Y%m%d"),
        "end": end_date.strftime("%Y%m%d"),
        "format": "JSON",
    }

    r = requests.get(url, params=params, timeout=90)
    r.raise_for_status()

    data = r.json()["properties"]["parameter"][POWER_PARAM]

    df = pd.DataFrame({
        "data": pd.to_datetime(list(data.keys()), format="%Y%m%d"),
        "precipitacao_mm": list(data.values())
    })

    df["precipitacao_mm"] = pd.to_numeric(df["precipitacao_mm"], errors="coerce")
    return df


def date_steps(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=FIRMS_DAY_RANGE)


def get_firms(map_key, bbox, start_date, end_date):
    dfs = []

    for source in FIRMS_SOURCES:
        for current_date in date_steps(start_date, end_date):
            url = (
                "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                f"{map_key}/{source}/{bbox}/{FIRMS_DAY_RANGE}/{current_date.isoformat()}"
            )
            try:
                r = requests.get(url, timeout=90)
                r.raise_for_status()

                if not r.text.strip():
                    continue

                df = pd.read_csv(io.StringIO(r.text))

                if df.empty or "latitude" not in df.columns or "longitude" not in df.columns:
                    continue

                df["fonte"] = "NASA FIRMS"
                df["sensor"] = source
                dfs.append(df)

            except Exception as e:
                st.warning(f"FIRMS sem retorno para {source} em {current_date}: {e}")

    if not dfs:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=CRS_GEO)

    df_all = pd.concat(dfs, ignore_index=True).drop_duplicates()

    return gpd.GeoDataFrame(
        df_all,
        geometry=gpd.points_from_xy(df_all["longitude"], df_all["latitude"]),
        crs=CRS_GEO
    )


def load_inpe(uploaded_file):
    if uploaded_file is None:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=CRS_GEO)

    name = uploaded_file.name.lower()

    if name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)

    elif name.endswith(".xlsx"):
        df = pd.read_excel(uploaded_file)

    elif name.endswith((".geojson", ".json")):
        gdf = gpd.read_file(uploaded_file)
        if gdf.crs is None:
            gdf = gdf.set_crs(CRS_GEO)
        gdf = gdf.to_crs(CRS_GEO)
        gdf["fonte"] = "BD Queimadas INPE"
        return gdf

    elif name.endswith(".zip"):
        temp_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(uploaded_file, "r") as z:
            z.extractall(temp_dir)

        shp_files = []
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.lower().endswith(".shp"):
                    shp_files.append(os.path.join(root, f))

        if not shp_files:
            raise ValueError("Nenhum shapefile encontrado no ZIP do INPE.")

        gdf = gpd.read_file(shp_files[0])
        if gdf.crs is None:
            gdf = gdf.set_crs(CRS_GEO)

        gdf = gdf.to_crs(CRS_GEO)
        gdf["fonte"] = "BD Queimadas INPE"
        return gdf

    else:
        raise ValueError("Formato INPE não suportado.")

    cols = {c.lower(): c for c in df.columns}

    lat_col = next((cols[c] for c in ["latitude", "lat", "y"] if c in cols), None)
    lon_col = next((cols[c] for c in ["longitude", "lon", "long", "x"] if c in cols), None)

    if lat_col is None or lon_col is None:
        raise ValueError("O arquivo INPE precisa ter latitude e longitude.")

    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
    df = df.dropna(subset=[lat_col, lon_col])

    gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon_col], df[lat_col]), crs=CRS_GEO)
    gdf["fonte"] = "BD Queimadas INPE"
    gdf["latitude"] = df[lat_col]
    gdf["longitude"] = df[lon_col]
    return gdf


def filter_inpe_period(gdf, start_date, end_date):
    if gdf is None or gdf.empty:
        return gdf

    candidates = ["data", "date", "acq_date", "Data", "DataHora", "datahora", "data_hora", "datetime", "dt_foco", "data_pas"]

    date_col = None
    for c in candidates:
        if c in gdf.columns:
            date_col = c
            break

    if date_col is None:
        return gdf.copy()

    gdf = gdf.copy()
    gdf[date_col] = pd.to_datetime(gdf[date_col], errors="coerce")

    return gdf[(gdf[date_col].dt.date >= start_date) & (gdf[date_col].dt.date <= end_date)].copy()


def create_map():
    gdf_area = st.session_state.gdf_area
    gdf_buffer = st.session_state.gdf_buffer
    gdf_firms = st.session_state.gdf_firms
    gdf_inpe = st.session_state.gdf_inpe
    buffer_km = st.session_state.buffer_km
    info = st.session_state.area_info

    m = folium.Map(location=[info["centroid_lat"], info["centroid_lon"]], zoom_start=9, tiles="OpenStreetMap")

    folium.GeoJson(
        gdf_area,
        name="Área de interesse",
        style_function=lambda x: {"color": "black", "weight": 3, "fillOpacity": 0.05},
    ).add_to(m)

    if gdf_buffer is not None:
        folium.GeoJson(
            gdf_buffer,
            name=f"Buffer {buffer_km} km",
            style_function=lambda x: {"color": "red", "weight": 2, "dashArray": "6,6", "fillOpacity": 0.03},
        ).add_to(m)

    if gdf_firms is not None and not gdf_firms.empty:
        for _, row in gdf_firms.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=4,
                color="red",
                fill=True,
                fill_opacity=0.8,
                popup=f"NASA FIRMS<br>{row.get('sensor', '')}<br>{row.get('classe_espacial', '')}",
            ).add_to(m)

    if gdf_inpe is not None and not gdf_inpe.empty:
        for _, row in gdf_inpe.iterrows():
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=4,
                color="orange",
                fill=True,
                fill_opacity=0.8,
                popup=f"BD Queimadas INPE<br>{row.get('classe_espacial', '')}",
            ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


def map_png():
    gdf_area = st.session_state.gdf_area
    gdf_buffer = st.session_state.gdf_buffer
    gdf_firms = st.session_state.gdf_firms
    gdf_inpe = st.session_state.gdf_inpe

    fig, ax = plt.subplots(figsize=(8, 8))

    if gdf_buffer is not None:
        gdf_buffer.plot(ax=ax, facecolor="none", edgecolor="red", linestyle="--", linewidth=1.5, label="Buffer")

    gdf_area.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2, label="Área")

    if gdf_firms is not None and not gdf_firms.empty:
        gdf_firms.plot(ax=ax, color="red", markersize=25, alpha=0.8, label="NASA FIRMS")

    if gdf_inpe is not None and not gdf_inpe.empty:
        gdf_inpe.plot(ax=ax, color="orange", markersize=22, alpha=0.8, label="INPE")

    ax.set_title("Área, buffer e focos de queimadas")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True)

    try:
        ax.legend()
    except Exception:
        pass

    temp_png = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(temp_png.name, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return temp_png.name


def chuva_png(df):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(df["data"], df["precipitacao_mm"], marker="o", linewidth=1)
    ax.set_title("Precipitação diária - NASA POWER")
    ax.set_xlabel("Data")
    ax.set_ylabel("mm/dia")
    ax.grid(True)

    temp_png = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(temp_png.name, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return temp_png.name


def count_points(gdf, classe):
    if gdf is None or gdf.empty or "classe_espacial" not in gdf.columns:
        return 0
    return int((gdf["classe_espacial"] == classe).sum())


def generate_pdf():
    pdf_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf_path = pdf_file.name

    doc = SimpleDocTemplate(pdf_path, pagesize=A4, rightMargin=35, leftMargin=35, topMargin=35, bottomMargin=35)
    styles = getSampleStyleSheet()
    story = []

    info = st.session_state.area_info
    buffer_km = st.session_state.buffer_km
    df_chuva = st.session_state.df_chuva
    gdf_firms = st.session_state.gdf_firms
    gdf_inpe = st.session_state.gdf_inpe
    periodo = st.session_state.periodo
    area_name = st.session_state.area_name

    if periodo:
        periodo_txt = f"{periodo[0]} a {periodo[1]}"
    else:
        periodo_txt = "Não informado"

    firms_dentro = count_points(gdf_firms, "Dentro do território")
    firms_entorno = count_points(gdf_firms, f"Entorno {buffer_km} km")
    inpe_dentro = count_points(gdf_inpe, "Dentro do território")
    inpe_entorno = count_points(gdf_inpe, f"Entorno {buffer_km} km")

    chuva_total = 0 if df_chuva is None or df_chuva.empty else float(df_chuva["precipitacao_mm"].sum())
    chuva_media = 0 if df_chuva is None or df_chuva.empty else float(df_chuva["precipitacao_mm"].mean())
    chuva_max = 0 if df_chuva is None or df_chuva.empty else float(df_chuva["precipitacao_mm"].max())

    story.append(Paragraph("Kw'y GeoMonitor", styles["Title"]))
    story.append(Paragraph("Relatório Técnico de Chuva e Queimadas", styles["Heading1"]))
    story.append(Spacer(1, 12))

    dados = [
        f"<b>Área analisada:</b> {area_name}",
        f"<b>Período:</b> {periodo_txt}",
        f"<b>Área total:</b> {info['area_ha']:.2f} ha",
        f"<b>Perímetro:</b> {info['perimetro_km']:.2f} km",
        f"<b>Buffer:</b> {buffer_km} km",
        f"<b>Centroide:</b> {info['centroid_lat']:.6f}, {info['centroid_lon']:.6f}",
        f"<b>BBOX:</b> {info['bbox']}",
    ]

    for d in dados:
        story.append(Paragraph(d, styles["Normal"]))

    story.append(Spacer(1, 12))

    mp = map_png()
    story.append(Paragraph("Mapa da área, buffer e focos", styles["Heading2"]))
    story.append(Image(mp, width=450, height=340))
    story.append(Spacer(1, 12))

    tabela = [
        ["Indicador", "Valor"],
        ["Chuva acumulada NASA POWER (mm)", round(chuva_total, 2)],
        ["Chuva média diária (mm/dia)", round(chuva_media, 2)],
        ["Maior chuva diária (mm)", round(chuva_max, 2)],
        ["FIRMS dentro", firms_dentro],
        [f"FIRMS entorno {buffer_km} km", firms_entorno],
        ["INPE dentro", inpe_dentro],
        [f"INPE entorno {buffer_km} km", inpe_entorno],
        ["Total dentro", firms_dentro + inpe_dentro],
        [f"Total entorno {buffer_km} km", firms_entorno + inpe_entorno],
    ]

    table = Table(tabela, colWidths=[310, 120])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))

    story.append(Paragraph("Resumo quantitativo", styles["Heading2"]))
    story.append(table)
    story.append(Spacer(1, 12))

    if df_chuva is not None and not df_chuva.empty:
        cp = chuva_png(df_chuva)
        story.append(Paragraph("Precipitação diária", styles["Heading2"]))
        story.append(Image(cp, width=450, height=240))
        story.append(Spacer(1, 12))

    fontes = [
        ["Fonte", "Uso"],
        ["NASA POWER", "Precipitação diária"],
        ["PRECTOTCORR", "Precipitação diária corrigida, mm/dia"],
        ["NASA FIRMS", "Focos de calor por satélite"],
        ["Sensores FIRMS", ", ".join(FIRMS_SOURCES)],
        ["BD Queimadas INPE", "Registros geolocalizados enviados pelo usuário"],
        ["Arquivo vetorial", "Limite territorial e buffer"],
    ]

    tf = Table(fontes, colWidths=[160, 300])
    tf.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))

    story.append(Paragraph("Bases de dados e parâmetros utilizados", styles["Heading2"]))
    story.append(tf)
    story.append(Spacer(1, 12))

    if firms_dentro + inpe_dentro == 0 and firms_entorno + inpe_entorno > 0:
        interpretacao = f"Não foram identificados focos dentro do território, porém houve focos no entorno de {buffer_km} km."
    elif firms_dentro + inpe_dentro == 0:
        interpretacao = f"Não foram identificados focos dentro do território nem no entorno de {buffer_km} km."
    else:
        interpretacao = "Foram identificados focos dentro do território, recomendando validação e avaliação de impactos."

    story.append(Paragraph("Descritivo técnico", styles["Heading2"]))
    story.append(Paragraph(
        f"A análise integrou NASA POWER, NASA FIRMS e BD Queimadas INPE. "
        f"A área foi analisada em EPSG:4326, com buffer externo de {buffer_km} km. "
        f"{interpretacao}",
        styles["Normal"]
    ))

    doc.build(story)

    with open(pdf_path, "rb") as f:
        return f.read()


init_state()

st.title("🔥 Kw'y GeoMonitor")
st.caption("Monitoramento ambiental da TI Xikrin do Cateté")

page = st.sidebar.radio(
    "Menu",
    ["Dashboard", "Área", "Chuva e Queimadas", "Relatório"]
)

if page == "Dashboard":
    st.header("📊 Dashboard")

    if not st.session_state.area_loaded:
        st.info("Carregue primeiro uma área na aba Área.")
    else:
        info = st.session_state.area_info

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Área", f"{info['area_ha']:.2f} ha")
        c2.metric("Perímetro", f"{info['perimetro_km']:.2f} km")
        c3.metric("Polígonos", info["poligonos"])
        c4.metric("Buffer", f"{st.session_state.buffer_km} km")

        if st.session_state.df_chuva is not None:
            st.metric("Chuva acumulada", f"{st.session_state.df_chuva['precipitacao_mm'].sum():.2f} mm")

elif page == "Área":
    st.header("🗺️ Área do Projeto")

    st.session_state.area_name = st.text_input("Nome da área", st.session_state.area_name)

    if st.session_state.area_loaded:
        st.success("Área carregada e salva na sessão.")

        info = st.session_state.area_info

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Área", f"{info['area_ha']:.2f} ha")
        c2.metric("Perímetro", f"{info['perimetro_km']:.2f} km")
        c3.metric("Polígonos", info["poligonos"])
        c4.metric("Buffer", f"{st.session_state.buffer_km} km")

        st.code(f"Centroide: {info['centroid_lat']:.6f}, {info['centroid_lon']:.6f}\nBBOX: {info['bbox']}")

        st_folium(create_map(), width=None, height=600)

        if st.button("Trocar área"):
            for k in ["area_loaded", "gdf_area", "gdf_buffer", "area_info", "df_chuva", "gdf_firms", "gdf_inpe", "periodo", "pdf_bytes"]:
                st.session_state[k] = None
            st.session_state.area_loaded = False
            st.rerun()

    else:
        uploaded = st.file_uploader("Envie GeoJSON ou Shapefile ZIP", type=["geojson", "json", "zip"])

        buffer_km = st.number_input("Buffer de entorno (km)", 0.0, 100.0, float(st.session_state.buffer_km), 1.0)

        if uploaded:
            try:
                gdf_area = load_area(uploaded)
                info = get_area_info(gdf_area)
                gdf_buffer = create_buffer(gdf_area, buffer_km) if buffer_km > 0 else None

                st.session_state.gdf_area = gdf_area
                st.session_state.gdf_buffer = gdf_buffer
                st.session_state.area_info = info
                st.session_state.buffer_km = buffer_km
                st.session_state.area_loaded = True

                st.success("Área carregada.")
                st.rerun()

            except Exception as e:
                st.error(f"Erro ao carregar área: {e}")

elif page == "Chuva e Queimadas":
    st.header("🌧️🔥 Chuva e Queimadas")

    if not st.session_state.area_loaded:
        st.warning("Carregue primeiro a área.")
        st.stop()

    info = st.session_state.area_info

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Data inicial", date(date.today().year, 1, 1))
    with col2:
        end_date = st.date_input("Data final", date.today())

    firms_key = st.text_input("Chave NASA FIRMS", type="password")
    inpe_file = st.file_uploader("BD Queimadas INPE", type=["csv", "xlsx", "geojson", "json", "zip"])

    if st.button("Processar análise"):
        with st.spinner("Baixando chuva NASA POWER..."):
            df_chuva = get_chuva(info["centroid_lat"], info["centroid_lon"], start_date, end_date)

        with st.spinner("Consultando NASA FIRMS..."):
            if firms_key:
                gdf_firms = get_firms(firms_key, info["bbox"], start_date, end_date)
                gdf_firms = classify_points(gdf_firms, st.session_state.gdf_area, st.session_state.gdf_buffer, st.session_state.buffer_km)
            else:
                gdf_firms = None

        with st.spinner("Carregando BD Queimadas INPE..."):
            gdf_inpe = load_inpe(inpe_file)
            gdf_inpe = filter_inpe_period(gdf_inpe, start_date, end_date)
            gdf_inpe = classify_points(gdf_inpe, st.session_state.gdf_area, st.session_state.gdf_buffer, st.session_state.buffer_km)

        st.session_state.df_chuva = df_chuva
        st.session_state.gdf_firms = gdf_firms
        st.session_state.gdf_inpe = gdf_inpe
        st.session_state.periodo = (start_date, end_date)
        st.session_state.pdf_bytes = None
        st.success("Análise processada.")

    if st.session_state.df_chuva is not None:
        df_chuva = st.session_state.df_chuva
        gdf_firms = st.session_state.gdf_firms
        gdf_inpe = st.session_state.gdf_inpe
        buffer_km = st.session_state.buffer_km

        firms_dentro = count_points(gdf_firms, "Dentro do território")
        firms_entorno = count_points(gdf_firms, f"Entorno {buffer_km} km")
        inpe_dentro = count_points(gdf_inpe, "Dentro do território")
        inpe_entorno = count_points(gdf_inpe, f"Entorno {buffer_km} km")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Chuva acumulada", f"{df_chuva['precipitacao_mm'].sum():.2f} mm")
        c2.metric("FIRMS dentro", firms_dentro)
        c3.metric("INPE dentro", inpe_dentro)
        c4.metric(f"Entorno {buffer_km} km", firms_entorno + inpe_entorno)

        st.subheader("Mapa")
        st_folium(create_map(), width=None, height=600)

        st.subheader("Precipitação NASA POWER")
        st.line_chart(df_chuva.set_index("data")["precipitacao_mm"])
        st.dataframe(df_chuva, use_container_width=True)

        st.subheader("NASA FIRMS")
        if gdf_firms is None:
            st.info("Chave FIRMS não informada.")
        elif gdf_firms.empty:
            st.info("Nenhum foco FIRMS encontrado.")
        else:
            st.dataframe(pd.DataFrame(gdf_firms.drop(columns="geometry")), use_container_width=True)

        st.subheader("BD Queimadas INPE")
        if gdf_inpe is None or gdf_inpe.empty:
            st.info("Nenhum foco INPE encontrado.")
        else:
            st.dataframe(pd.DataFrame(gdf_inpe.drop(columns="geometry")), use_container_width=True)

elif page == "Relatório":
    st.header("📄 Relatório PDF")

    if not st.session_state.area_loaded:
        st.warning("Carregue primeiro a área.")
        st.stop()

    c1, c2, c3 = st.columns(3)
    c1.metric("NASA POWER", "Sim" if st.session_state.df_chuva is not None else "Não")
    c2.metric("NASA FIRMS", "Sim" if st.session_state.gdf_firms is not None else "Não")
    c3.metric("BD INPE", "Sim" if st.session_state.gdf_inpe is not None else "Não")

    if st.button("Gerar relatório PDF"):
        st.session_state.pdf_bytes = generate_pdf()
        st.success("Relatório gerado.")

    if st.session_state.pdf_bytes:
        st.download_button(
            "Baixar relatório PDF",
            data=st.session_state.pdf_bytes,
            file_name="relatorio_kwy_geomonitor.pdf",
            mime="application/pdf"
        )
