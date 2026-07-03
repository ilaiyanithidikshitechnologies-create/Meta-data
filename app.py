import streamlit as st
import pandas as pd
import threading

# Reload crawler on every run so Streamlit hot-reload picks up changes
import importlib, crawler
importlib.reload(crawler)
from crawler import crawl_site

st.set_page_config(
    page_title="Meta Data Scanner (MDS)",
    page_icon="🔍",
    layout="wide",
)

# ── Header ────────────────────────────────────────────────────────────────────
st.title("🔍 Meta Data Scanner (MDS)")
st.subheader("Website Meta Title & Description Extractor")
st.markdown("Supports **static sites**, **SSR**, and **React / SPA** sites.")

# ── Inputs ────────────────────────────────────────────────────────────────────
col1, col2 = st.columns([3, 1])
with col1:
    url = st.text_input("Enter Website URL", "https://www.dikshitech.com", label_visibility="collapsed")
with col2:
    max_pages = st.number_input("Max pages", min_value=1, max_value=1000, value=500, step=50)

scan_col, cancel_col = st.columns([1, 5])
scan_btn   = scan_col.button("🚀 Scan Website", use_container_width=True)
cancel_btn = cancel_col.button("⛔ Cancel", use_container_width=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "scan_results" not in st.session_state:
    st.session_state.scan_results = None
if "cancel_flag" not in st.session_state:
    st.session_state.cancel_flag = threading.Event()

if cancel_btn and st.session_state.scan_running:
    st.session_state.cancel_flag.set()
    st.warning("⛔ Cancelling scan…")

# ── Scan ──────────────────────────────────────────────────────────────────────
if scan_btn and not st.session_state.scan_running:
    st.session_state.scan_running = True
    st.session_state.scan_results = None
    st.session_state.cancel_flag.clear()

    progress_bar   = st.progress(0, text="⏳ Initializing scan…")
    status_text    = st.empty()

    def update_progress(pct: int, text: str):
        # Bail out early if user clicked Cancel
        if st.session_state.cancel_flag.is_set():
            return
        try:
            progress_bar.progress(pct, text=f"⏳ {text}")
            status_text.caption(text)
        except Exception:
            pass

    try:
        data = crawl_site(
            url,
            max_pages=int(max_pages),
            progress_callback=update_progress,
        )
        st.session_state.scan_results = data
    except Exception as e:
        st.error(f"Scan error: {e}")
    finally:
        st.session_state.scan_running = False

    progress_bar.progress(100, text="✅ Scan complete!")
    status_text.empty()

# ── Results ───────────────────────────────────────────────────────────────────
if st.session_state.scan_results is not None:
    data = st.session_state.scan_results

    if not data:
        st.error("No pages found. Check the URL and try again.")
    else:
        df = pd.DataFrame(data)

        # Summary metrics
        total   = len(df)
        no_title = int((df["Meta Title"] == "").sum())
        no_desc  = int((df["Meta Description"] == "").sum())
        errors   = int((df["Indexability"] == "Error").sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Pages",        total)
        m2.metric("Missing Title",       no_title,  delta=f"-{no_title}"  if no_title  else None, delta_color="inverse")
        m3.metric("Missing Description", no_desc,   delta=f"-{no_desc}"   if no_desc   else None, delta_color="inverse")
        m4.metric("Errors",              errors,    delta=f"-{errors}"    if errors    else None, delta_color="inverse")

        st.divider()

        # Colour-code rows with issues
        def _highlight(row):
            if row.get("Indexability") == "Error":
                return ["background-color: #3d1f1f"] * len(row)
            if row.get("Meta Title") == "" or row.get("Meta Description") == "":
                return ["background-color: #3d3414"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df.style.apply(_highlight, axis=1),
            use_container_width=True,
            height=500,
        )

        # Download
        file_name = "Metadata_Report.xlsx"
        df.to_excel(file_name, index=False)
        with open(file_name, "rb") as f:
            st.download_button(
                "📥 Download Excel Report",
                f,
                file_name=file_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=False,
            )
