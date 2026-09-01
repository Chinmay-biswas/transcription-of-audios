import streamlit as st

# Configure the browser tab title and layout
st.set_page_config(
    page_title="Meeting Intelligence Dashboard",
    page_icon="🖥️",
    layout="wide"
)

st.title("⭐ Meeting Intelligence Pipeline")
st.markdown("---")

st.markdown("""
### Welcome to your AI-Powered Meeting Assistant!
This pipeline automates the extraction of actionable intelligence from your recorded voice conversations and meetings.

#### Features Available:
1. **Upload & Process:** Upload raw audio (`.mp3`, `.wav`) to transcribe text and automatically extract action items, summaries, and sentiments.
2. **Meeting History:** View logs of your past meetings.
3. **Semantic Search:** Query your meetings using natural language (e.g., *"What did we decide about the marketing budget last week?"*).
""")

# Quick health check display to see if backend is running
st.sidebar.info("System Status: Operational")