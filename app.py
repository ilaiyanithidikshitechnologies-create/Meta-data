import streamlit as st
import pandas as pd
from crawler import crawl_site

st.set_page_config(
    page_title="Meta Text (MT)",
    layout="wide"
)

st.title("🟢 Meta Text (MT)")
st.subheader("Website Meta Title & Description Extractor")

url = st.text_input(
    "Enter Website URL",
    "https://www.dikshitech.com"
)

if st.button("🚀 Scan Website"):

    with st.spinner("Scanning Website..."):

        data = crawl_site(url)

    if len(data) == 0:

        st.error("No pages found.")

    else:

        df = pd.DataFrame(data)

        st.success(f"Total Pages Found : {len(df)}")

        st.dataframe(df, use_container_width=True)

        file_name = "Meta_Report.xlsx"

        df.to_excel(file_name, index=False)

        with open(file_name, "rb") as f:

            st.download_button(

                "📥 Download Excel",

                f,

                file_name=file_name,

                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

            )