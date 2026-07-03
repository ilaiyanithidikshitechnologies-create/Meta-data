import streamlit as st
import pandas as pd
import importlib
import crawler
importlib.reload(crawler)
from crawler import crawl_site

st.set_page_config(
    page_title="Meta Data Scanner (MDS)",
    layout="wide"
)

st.title("🟢 Meta Data Scanner (MDS)")
st.subheader("Website Meta Title & Description Extractor")

url = st.text_input(
    "Enter Website URL",
    "https://www.dikshitech.com"
)

if st.button("🚀 Scan Website"):

    progress_bar = st.progress(0, text="⏳ Initializing scan...")

    def update_progress(pct, text):
        try:
            progress_bar.progress(min(max(int(pct), 0), 100), text=f"⏳ {text}")
        except Exception:
            pass

    data = crawl_site(url, progress_callback=update_progress)

    progress_bar.progress(100, text="✅ Scan Complete!")

    if len(data) == 0:

        st.error("No pages found.")

    else:

        df = pd.DataFrame(data)

        st.success(f"Total Pages Found : {len(df)}")

        st.dataframe(df, use_container_width=True)

        file_name = "Metadata_Report.xlsx"

        df.to_excel(file_name, index=False)

        with open(file_name, "rb") as f:

            st.download_button(

                "📥 Download Excel",

                f,

                file_name=file_name,

                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

            )