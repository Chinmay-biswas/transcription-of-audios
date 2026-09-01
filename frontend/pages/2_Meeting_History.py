import streamlit as st
import requests

# 1. Page Configuration
st.set_page_config(page_title="Chat with Meeting", page_icon="💬", layout="wide")

st.title("💬 Chat with your Meetings")
st.markdown("Select a past meeting from your archive and ask questions about what was discussed.")

# Define the base URL for your FastAPI backend
API_BASE = "http://127.0.0.1:8000/api/v1"

# --- 2. Fetch Available Meetings ---
# We use @st.cache_data so it doesn't spam the backend API on every single keystroke/refresh
@st.cache_data(ttl=60)
def get_meeting_list():
    try:
        response = requests.get(f"{API_BASE}/meetings")
        if response.status_code == 200:
            return response.json().get("meetings", [])
    except requests.exceptions.ConnectionError:
        return []
    return []

available_meetings = get_meeting_list()

if not available_meetings:
    st.info("No meetings found in the database. Please upload and process an audio file first!")
else:
    # --- 3. Meeting Selector ---
    selected_meeting = st.selectbox(
        "Select a meeting to analyze:",
        available_meetings,
        help="This will filter the AI's memory to ONLY this specific file."
    )

    st.markdown("---")

    # --- 4. Chat Session State Initialization ---
    # Streamlit reruns the script on every interaction. We use session_state to remember
    # the chat history so it doesn't disappear when the user types a new message.
    if "messages" not in st.session_state or st.session_state.get("current_meeting") != selected_meeting:
        st.session_state.messages = []
        st.session_state.current_meeting = selected_meeting # Reset chat if they pick a different meeting

    # Display existing chat messages from the session state
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # --- 5. The RAG Chat Input & Execution ---
    # The ":=" operator assigns the input to 'prompt' and checks if it's not empty simultaneously
    if prompt := st.chat_input(f"Ask a question about {selected_meeting}..."):

        # Add user's message to the UI and session state immediately
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Generate AI response
        with st.chat_message("assistant"):
            with st.spinner("Searching transcripts and thinking..."):
                try:
                    # Send the query AND the selected meeting ID to the backend
                    payload = {"query": prompt, "meeting_id": selected_meeting}
                    response = requests.post(f"{API_BASE}/meeting-chat", json=payload)

                    if response.status_code == 200:
                        data = response.json()
                        answer = data.get("answer", "Error generating answer.")

                        # Display the answer
                        st.markdown(answer)

                        # Show the exact chunks pulled from ChromaDB to prove the AI isn't hallucinating
                        with st.expander("🔍 View Source Transcript Context"):
                            for idx, chunk in enumerate(data.get("context_used", [])):
                                st.caption(f"**Chunk {idx+1}:** {chunk}")

                        # Save the AI's response to the chat history
                        st.session_state.messages.append({"role": "assistant", "content": answer})
                    else:
                        st.error(f"Backend API Error: {response.text}")
                except Exception as e:
                    st.error(f"Failed to connect to backend: {e}")