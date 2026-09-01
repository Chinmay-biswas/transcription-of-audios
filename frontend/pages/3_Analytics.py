import streamlit as st
import requests
import ast
import pandas as pd
import plotly.express as px

st.set_page_config(page_title="Task Analytics", page_icon="📊", layout="wide")

st.title("⭐ Pipeline Operations & Task Analytics")
st.markdown("A global overview of cross-meeting execution accountability, priorities, and workflow pipelines.")

SEARCH_URL = "http://127.0.0.1:8000/api/v1/search"

# We execute an empty or wide-scoped vector scan query to extract general historical records
with st.spinner("Compiling cross-meeting tracking details..."):
    try:
        # Pass a generic wildcard concept string to bring back historical metadata blocks
        response = requests.post(SEARCH_URL, json={"query": "meeting summary action items tasks"})

        if response.status_code == 200:
            data = response.json()
            metadatas = data.get("results", {}).get("metadatas", [[]])[0]

            all_action_items = []
            sentiment_counts = {}

            # Loop through individual records and flatten arrays out into rows
            for meta in metadatas:
                try:
                    summary_dict = ast.literal_eval(meta.get("summary", "{}"))
                except Exception:
                    continue

                # Capture metadata operational metrics
                sent = summary_dict.get("overall_sentiment", "Neutral")
                sentiment_counts[sent] = sentiment_counts.get(sent, 0) + 1

                meeting_name = meta.get("filename", "Unknown")
                items = summary_dict.get("action_items", [])

                for item in items:
                    item['Source Meeting'] = meeting_name
                    all_action_items.append(item)

            if not all_action_items:
                st.info("No recorded operational actions yet found in your system. Process audio files first!")
            else:
                # Build unified analytical tracking dataframe
                df = pd.DataFrame(all_action_items)

                # Standardize styling headers
                df.rename(columns={
                    'task': 'Task Description',
                    'assignee': 'Assignee Name',
                    'due_date': 'Stated Deadline',
                    'priority': 'Priority Level'
                }, inplace=True, errors='ignore')

                # Replace None objects gracefully
                df.fillna("Unassigned", inplace=True)

                # Top Metrics Blocks Row
                m1, m2, m3 = st.columns(3)
                m1.metric("📋 Total Tasks Extracted", len(df))
                m2.metric("👥 Unique Active Assignees", df['Assignee Name'].nunique())
                m3.metric("📂 Total Logged Audio Files", len(metadatas))

                st.markdown("---")

                # Layout Visualization Graphics
                g1, g2 = st.columns(2)

                with g1:
                    st.subheader("🎯 Work Distribution Matrix")
                    assignee_counts = df['Assignee Name'].value_counts().reset_index()
                    assignee_counts.columns = ['Assignee', 'Task Count']
                    fig_assignee = px.bar(assignee_counts, x='Assignee', y='Task Count', text_auto=True)
                    st.plotly_chart(fig_assignee, use_container_width=True)

                with g2:
                    st.subheader("⚠️ Task Urgency Profiles")
                    priority_counts = df['Priority Level'].value_counts().reset_index()
                    priority_counts.columns = ['Priority', 'Count']
                    fig_priority = px.pie(priority_counts, values='Count', names='Priority', hole=0.4)
                    st.plotly_chart(fig_priority, use_container_width=True)

                st.markdown("### 📋 Unified Master Action Items Ledger")
                st.dataframe(df, use_container_width=True, hide_index=True)

        else:
            st.error("Failed tracking pipeline metrics connection.")
    except Exception as e:
        st.error(f"Please verify backend server status on Port 8000. Operational Link missing: {str(e)}")