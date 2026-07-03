import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from modules.db import read_alerts

st.set_page_config(page_title="Monitoramento Automático", page_icon="🔥", layout="wide")

st.title("🔥 Monitoramento Automático")
st.caption("Alertas gerados automaticamente pelo GitHub Actions a cada 6 horas")

df = read_alerts()

if df.empty:
    st.info("Ainda não há alertas registrados no banco.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)

c1.metric("Total de alertas", len(df))
c2.metric("Críticos", int((df["status_alerta"] == "CRITICO").sum()))
c3.metric("Preventivos", int((df["status_alerta"] == "PREVENTIVO").sum()))
c4.metric("Fontes", df["fonte"].nunique() if "fonte" in df.columns else 0)

st.subheader("🗺️ Mapa dos alertas")

lat_center = df["latitude"].mean()
lon_center = df["longitude"].mean()

m = folium.Map(location=[lat_center, lon_center], zoom_start=8, tiles="OpenStreetMap")

for _, row in df.iterrows():
    status = row.get("status_alerta", "")
    color = "red" if status == "CRITICO" else "orange"

    popup = f"""
    <b>Status:</b> {status}<br>
    <b>Fonte:</b> {row.get('fonte', '')}<br>
    <b>Sensor:</b> {row.get('sensor', '')}<br>
    <b>Data:</b> {row.get('data_foco', '')} {row.get('hora_foco', '')}<br>
    <b>FRP:</b> {row.get('frp', '')}<br>
    <b>Classe:</b> {row.get('classe_espacial', '')}<br>
    <b>Distância borda:</b> {row.get('distancia_borda_km', '')} km
    """

    folium.CircleMarker(
        location=[row["latitude"], row["longitude"]],
        radius=5,
        color=color,
        fill=True,
        fill_opacity=0.8,
        popup=popup
    ).add_to(m)

st_folium(m, width=None, height=600)

st.subheader("📋 Histórico de alertas")
st.dataframe(df, use_container_width=True)

st.download_button(
    "⬇️ Baixar histórico CSV",
    data=df.to_csv(index=False).encode("utf-8-sig"),
    file_name="historico_alertas_kwy_geomonitor.csv",
    mime="text/csv"
)
