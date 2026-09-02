import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import re
import io
import requests
import base64

# ================= 1. การตั้งค่าหน้าจอและ CSS =================
st.set_page_config(page_title="SPC Delivery Performance Dashboard", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 1.5rem; }
    h1 { color: #1E3A8A; font-weight: 600; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
    div[data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; color: #111827; }
    div[data-testid="stMetricLabel"] { font-size: 1rem; color: #6B7280; font-weight: 600; }
    </style>
""", unsafe_allow_html=True)

if 'df_merged' not in st.session_state:
    st.session_state.df_merged = pd.DataFrame()

# ================= 2. ฟังก์ชันประมวลผลข้อมูล และ GitHub API =================
def get_target_sheet(xls):
    sheet_names = xls.sheet_names
    rev_sheets = {sheet: int(match.group(1)) for sheet in sheet_names if (match := re.search(r'rev\.?\s*(\d+)', sheet, re.IGNORECASE))}
    if rev_sheets: return max(rev_sheets, key=rev_sheets.get)
    return next((s for s in sheet_names if 'ประมาณการ' in s), sheet_names[0])

def standardize_sub_name(name):
    name = str(name).strip().upper()
    mapping = {
        'CRM': ['CRM', 'CRM1'], 'KSA': ['KSA', 'KSA1'], 'MCR': ['MCR', 'MCR1'],
        'PKPS': ['PKPS', 'PK.PS'], 'PS': ['PS', 'PS1', 'PS01'], 'SCC': ['SCC', 'SCC1'],
        'SCR': ['SCR', 'SCR1'], 'TEP': ['TEP', 'TEP1'], 'TPS': ['TPS', 'TPS1'],
        'VK': ['VK', 'VK1', 'VK01'], 'WCL': ['WCL', 'WCL1', 'WCL01', 'WCL2']
    }
    for standard_name, variants in mapping.items():
        if name in variants: return standard_name
    return name

@st.cache_data
def process_data(plan_files, actual_file):
    df_plan_list = []
    for file in plan_files:
        xls = pd.ExcelFile(file)
        target_sheet = get_target_sheet(xls)
        df = pd.read_excel(xls, sheet_name=target_sheet)
        headers = [h.strftime('%Y-%m-%d') if isinstance(h, pd.Timestamp) else h.split(' ')[0] if isinstance(h, str) and '00:00:00' in h else str(h) for h in df.iloc[1].tolist()]
        df = df.iloc[2:].copy()
        df.columns = headers
        
        date_cols = [c for c in headers if re.match(r'\d{4}-\d{2}-\d{2}', str(c))]
        if 'Part No.' in df.columns:
            df_melted = df.melt(id_vars=['Part No.'], value_vars=date_cols, var_name='Date', value_name='Plan_Qty')
            df_melted['Subcontractor'] = standardize_sub_name(file.name.split(' ')[0])
            df_melted = df_melted.dropna(subset=['Part No.'])
            df_melted['Plan_Qty'] = pd.to_numeric(df_melted['Plan_Qty'], errors='coerce').fillna(0)
            df_melted = df_melted[df_melted['Plan_Qty'] > 0]
            df_melted['Part'] = df_melted['Part No.'].astype(str).str.strip()
            df_melted['Date'] = pd.to_datetime(df_melted['Date'], errors='coerce')
            df_plan_list.append(df_melted[['Date', 'Subcontractor', 'Part', 'Plan_Qty']])
            
    df_plan = pd.concat(df_plan_list) if df_plan_list else pd.DataFrame()

    if actual_file:
        df_actual_raw = pd.read_excel(actual_file)
        df_actual = df_actual_raw.iloc[:, [0, 2, 6, 10]].copy()
        df_actual.columns = ['Part', 'Sub_Code_Raw', 'Actual_Qty', 'Date']
        df_actual['Part'] = df_actual['Part'].astype(str).str.strip()
        df_actual['Date'] = pd.to_datetime(df_actual['Date'], errors='coerce')
        df_actual['Subcontractor'] = df_actual['Sub_Code_Raw'].apply(lambda x: standardize_sub_name(str(x).split('/')[2]) if len(str(x).split('/')) > 2 else 'Unknown')
        df_actual['Actual_Qty'] = pd.to_numeric(df_actual['Actual_Qty'], errors='coerce').fillna(0)
        df_actual = df_actual.groupby(['Date', 'Subcontractor', 'Part'])['Actual_Qty'].sum().reset_index()
    else:
        df_actual = pd.DataFrame(columns=['Date', 'Subcontractor', 'Part', 'Actual_Qty'])

    if not df_plan.empty and not df_actual.empty:
        df_merged = pd.merge(df_plan, df_actual, on=['Date', 'Subcontractor', 'Part'], how='outer').fillna(0)
    elif not df_plan.empty:
        df_merged = df_plan.copy(); df_merged['Actual_Qty'] = 0
    elif not df_actual.empty:
        df_merged = df_actual.copy(); df_merged['Plan_Qty'] = 0
    else:
        return pd.DataFrame()

    df_merged = df_merged[(df_merged['Plan_Qty'] > 0) | (df_merged['Actual_Qty'] > 0)]
    df_merged['Diff'] = df_merged['Actual_Qty'] - df_merged['Plan_Qty']
    df_merged['Status'] = np.where(df_merged['Diff'] >= 0, '✅ On Target', '❌ Shortage')
    df_merged['Achievement %'] = np.where(df_merged['Plan_Qty'] > 0, (df_merged['Actual_Qty'] / df_merged['Plan_Qty']) * 100, 100)
    return df_merged

def save_to_github(df, token, repo, path, branch):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    
    response = requests.get(url, headers=headers, params={"ref": branch})
    sha = response.json().get('sha') if response.status_code == 200 else None

    csv_data = df.to_csv(index=False).encode('utf-8')
    b64_content = base64.b64encode(csv_data).decode('utf-8')
    data = {"message": "Auto-save dashboard data via Streamlit", "content": b64_content, "branch": branch}
    if sha: data["sha"] = sha

    put_response = requests.put(url, headers=headers, json=data)
    return put_response.status_code in [200, 201, 202]

def load_from_github(token, repo, path, branch):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(url, headers=headers, params={"ref": branch})
    if response.status_code == 200:
        content = base64.b64decode(response.json().get('content'))
        df = pd.read_csv(io.BytesIO(content))
        df['Date'] = pd.to_datetime(df['Date'])
        return df
    return None

# ================= 3. ส่วนแสดงผล Sidebar =================
st.title("🏭 SPC Delivery Performance Dashboard")
st.markdown("ระบบวิเคราะห์และติดตามสถานะการส่งมอบชิ้นงานของ Subcontractor (Plan vs Actual)")

with st.sidebar:
    st.header("⚙️ 1. อัปโหลดข้อมูลใหม่")
    plan_files = st.file_uploader("อัปโหลดไฟล์แผน (Plan)", accept_multiple_files=True, type=['xlsx'])
    actual_file = st.file_uploader("อัปโหลดไฟล์รับเข้า (Actual)", type=['xlsx'])
    
    if st.button("ประมวลผลไฟล์ Excel"):
        if plan_files and actual_file:
            with st.spinner("⏳ กำลังประมวลผล..."):
                st.session_state.df_merged = process_data(plan_files, actual_file)
                st.success("ประมวลผลสำเร็จ!")
        else:
            st.warning("กรุณาอัปโหลดไฟล์ทั้ง Plan และ Actual")

    st.markdown("---")
    st.header("☁️ 2. จัดการข้อมูลกับ GitHub (Cloud Sync)")
    
    try:
        gh_token = st.secrets["GITHUB_TOKEN"]
        gh_repo = st.secrets["GITHUB_REPO"]
        gh_branch = st.secrets.get("GITHUB_BRANCH", "main")
        gh_dir = st.secrets.get("GITHUB_DATA_DIR", "data")
        gh_path = f"{gh_dir}/saved_dashboard_data.csv"
        
        col_gh1, col_gh2 = st.columns(2)
        if col_gh1.button("💾 บันทึกการตั้งค่า"):
            if not st.session_state.df_merged.empty:
                with st.spinner("กำลังบันทึกข้อมูลลง GitHub..."):
                    if save_to_github(st.session_state.df_merged, gh_token, gh_repo, gh_path, gh_branch):
                        st.success("บันทึกสำเร็จ!")
                    else:
                        st.error("บันทึกไม่สำเร็จ ตรวจสอบสิทธิ์ (Permissions) ของ Token")
            else:
                st.warning("ไม่มีข้อมูลให้บันทึก")

        if col_gh2.button("📥 โหลดข้อมูลล่าสุด"):
            with st.spinner("กำลังดึงข้อมูลจาก GitHub..."):
                loaded_df = load_from_github(gh_token, gh_repo, gh_path, gh_branch)
                if loaded_df is not None:
                    st.session_state.df_merged = loaded_df
                    st.success("โหลดข้อมูลสำเร็จ!")
                else:
                    st.error("ไม่พบข้อมูลบน GitHub หรือ Repository ไม่ถูกต้อง")
    except KeyError:
        st.warning("⚠️ ยังไม่ได้ตั้งค่า Secrets กรุณาไปที่หน้า Settings ของ Streamlit Cloud เพื่อเพิ่ม GITHUB_TOKEN และ GITHUB_REPO")

    st.markdown("---")

# ================= 4. ส่วนแสดงผลหลัก (Main UI) =================
if not st.session_state.df_merged.empty:
    df_merged = st.session_state.df_merged
    min_date, max_date = df_merged['Date'].min().date(), df_merged['Date'].max().date()
    
    st.sidebar.subheader("📅 Date Range Filter")
    selected_dates = st.sidebar.date_input("เลือกช่วงวันที่", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    start_date, end_date = selected_dates if len(selected_dates) == 2 else (selected_dates[0], selected_dates[0])
    df_filtered_date = df_merged[(df_merged['Date'].dt.date >= start_date) & (df_merged['Date'].dt.date <= end_date)]
    
    st.sidebar.subheader("🔍 Data Filter")
    selected_sub = st.sidebar.multiselect("Subcontractor", options=sorted(df_filtered_date['Subcontractor'].unique()), default=df_filtered_date['Subcontractor'].unique())
    selected_status = st.sidebar.multiselect("Status", options=df_filtered_date['Status'].unique(), default=df_filtered_date['Status'].unique())
    
    df_filtered = df_filtered_date[(df_filtered_date['Subcontractor'].isin(selected_sub)) & (df_filtered_date['Status'].isin(selected_status))]

    total_plan = df_filtered['Plan_Qty'].sum()
    total_actual = df_filtered['Actual_Qty'].sum()
    total_diff = total_actual - total_plan
    achv_pct = (total_actual / total_plan * 100) if total_plan > 0 else 100
    
    st.markdown(f"<span style='color:#6B7280; font-size:14px;'>ข้อมูลตั้งแต่วันที่ <b>{start_date.strftime('%d/%m/%Y')}</b> ถึง <b>{end_date.strftime('%d/%m/%Y')}</b></span>", unsafe_allow_html=True)
    
    col1, col2, col3, col4 = st.columns(4)
    with col1: st.metric("📦 แผนทั้งหมด (Total Plan)", f"{total_plan:,.0f}")
    with col2: st.metric("🚚 รับเข้าจริง (Total Actual)", f"{total_actual:,.0f}")
    with col3: st.metric("⚖️ ส่วนต่าง (Variance)", f"{total_diff:,.0f}", delta=f"{total_diff:,.0f} Pcs", delta_color="normal")
    with col4: st.metric("🎯 ความสำเร็จ (Achievement)", f"{achv_pct:.1f}%")
    st.markdown("<br>", unsafe_allow_html=True)

    df_sub = df_filtered.groupby('Subcontractor').agg(Plan_Qty=('Plan_Qty', 'sum'), Actual_Qty=('Actual_Qty', 'sum')).reset_index()
    df_sub['Achievement %'] = np.where(df_sub['Plan_Qty'] > 0, (df_sub['Actual_Qty'] / df_sub['Plan_Qty']) * 100, 100.0)

    tab1, tab2, tab3 = st.tabs(["📊 Overview Performance", "🏆 Subcontractor Ranking", "📋 Detailed Data Log"])
    
    with tab1:
        st.markdown("### เปรียบเทียบแผนและการส่งมอบราย Subcontractor พร้อม % ความสำเร็จ")
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Bar(x=df_sub['Subcontractor'], y=df_sub['Plan_Qty'], name='Plan (แผน)', marker_color='#1E3A8A'), secondary_y=False)
        fig.add_trace(go.Bar(x=df_sub['Subcontractor'], y=df_sub['Actual_Qty'], name='Actual (รับจริง)', marker_color='#10B981'), secondary_y=False)
        fig.add_trace(
            go.Scatter(x=df_sub['Subcontractor'], y=df_sub['Achievement %'], name='Achievement %', mode='lines+markers+text', 
                       text=df_sub['Achievement %'].apply(lambda x: f"{x:.1f}%"), textposition="top center",
                       line=dict(color='#F59E0B', width=3), marker=dict(size=8, color='#F59E0B')),
            secondary_y=True
        )
        fig.update_layout(template='plotly_white', barmode='group', legend=dict(orientation="h", yanchor="bottom", y=1.1, xanchor="right", x=1), margin=dict(t=60, b=40, l=40, r=40), hovermode="x unified")
        fig.update_yaxes(title_text="จำนวนชิ้นงาน (Qty)", showgrid=True, gridwidth=1, gridcolor='#E5E7EB', secondary_y=False)
        max_pct = df_sub['Achievement %'].max()
        fig.update_yaxes(title_text="% ความสำเร็จ", showgrid=False, range=[0, max_pct * 1.15 if max_pct > 0 else 100], secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.markdown("### จัดอันดับประสิทธิภาพการส่งมอบ (Achievement %)")
        df_sub_sorted = df_sub.sort_values('Achievement %', ascending=False)
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            top_sub = df_sub_sorted.head(5).copy()
            fig_top = px.bar(top_sub, x='Achievement %', y='Subcontractor', orientation='h', text=top_sub['Achievement %'].apply(lambda x: f"{x:.1f}%"))
            fig_top.update_traces(marker_color='#10B981', textposition='outside')
            fig_top.update_layout(title="✨ Top 5: ส่งมอบได้ตามแผนดีที่สุด", template='plotly_white', yaxis={'categoryorder':'total ascending'})
            st.plotly_chart(fig_top, use_container_width=True)
            
        with col_chart2:
            worst_sub = df_sub_sorted.tail(5).copy()
            fig_worst = px.bar(worst_sub, x='Achievement %', y='Subcontractor', orientation='h', text=worst_sub['Achievement %'].apply(lambda x: f"{x:.1f}%"))
            fig_worst.update_traces(marker_color='#EF4444', textposition='outside')
            fig_worst.update_layout(title="⚠️ Bottom 5: ส่งไม่ได้ตามแผน (Shortage)", template='plotly_white', yaxis={'categoryorder':'total descending'})
            st.plotly_chart(fig_worst, use_container_width=True)

    with tab3:
        st.markdown("### ข้อมูลเชิงลึก (Raw Data)")
        df_display = df_filtered.copy()
        df_display['Date'] = df_display['Date'].dt.strftime('%Y-%m-%d')
        df_display = df_display.sort_values(by=['Date', 'Subcontractor', 'Part'])
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df_display.to_excel(writer, index=False, sheet_name='Detailed_Data')
        st.download_button(label="📥 ดาวน์โหลดข้อมูล (Export to Excel)", data=buffer.getvalue(), file_name="Subcontractor_Detailed_Data.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.dataframe(
            df_display.style.format({'Plan_Qty': '{:,.0f}', 'Actual_Qty': '{:,.0f}', 'Diff': '{:,.0f}', 'Achievement %': '{:.1f}%'})
            .map(lambda x: 'color: #EF4444' if x == '❌ Shortage' else 'color: #10B981', subset=['Status']),
            use_container_width=True, height=500
        )
else:
    st.info("👈 กรุณาประมวลผลไฟล์ Excel หรือโหลดข้อมูลจาก GitHub ทางด้านซ้ายมือเพื่อเริ่มต้น")