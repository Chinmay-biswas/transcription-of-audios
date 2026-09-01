import streamlit as st
import requests

st.set_page_config(page_title="Upload Audio", page_icon="🎙️", layout="wide")

st.title("⭐ Upload & Process Meeting Audio")
st.markdown("Transform raw audio recordings into structured, actionable items instantly.")

# Define the backend API URL (FastAPI default)
BACKEND_URL = "http://127.0.0.1:8000/api/v1/process-meeting"

# File Uploader Widget
uploaded_file = st.file_uploader(
    "Choose a meeting recording file",
    type=["mp3", "wav", "m4a"],
    help="Supported formats: MP3, WAV, M4A"
)

if uploaded_file is not None:
    st.audio(uploaded_file, format="audio/mp3")

    # Process Button
    if st.button("⭐ Run AI Pipeline", type="primary"):
        with st.spinner("Step 1/2: Transcribing Audio (Whisper)... \nStep 2/2: Extracting Intelligence (GPT-4o-mini)..."):
            try:
                # Prepare the payload to match FastAPI's UploadFile requirement
                files = {"file": (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type)}

                # Fire the HTTP POST request to the backend
                response = requests.post(BACKEND_URL, files=files)

                if response.status_code == 200:
                    data = response.json()
                    st.success("Processing complete!")

                    # Distribute columns for data presentation
                    col1, col2 = st.columns(2)

                    with col1:
                        st.subheader("Smart Transcript")
                        st.caption(f"File: {data['transcription']['filename']} | Duration: {data['transcription']['duration_seconds']}s")
                        st.text_area("Raw Text Output", data['transcription']['transcript_text'], height=400)

                    with col2:
                        st.subheader("Meeting Intelligence")
                        intel = data["intelligence"]

                        # Show Executive Summary
                        st.markdown(f"**Sentiment:** `{intel['overall_sentiment']}`")
                        st.markdown(f"**Executive Summary:**\n{intel['executive_summary']}")

                        # Show Key Decisions
                        st.markdown("**Key Decisions:**")
                        for decision in intel["key_decisions"]:
                            st.markdown(f"- {decision}")

                        # Show Action Items Table
                        st.markdown("**Extracted Action Items:**")
                        if intel["action_items"]:
                            st.table(intel["action_items"])
                        else:
                            st.info("No explicit action items found in this transcript.")

                else:
                    st.error(f"Backend Error ({response.status_code}): {response.text}")

            except requests.exceptions.ConnectionError:
                st.error("Could not connect to the Backend server. Make sure your FastAPI app is running on port 8000!")
            except Exception as e:
                st.error(f"An unexpected error occurred: {str(e)}")