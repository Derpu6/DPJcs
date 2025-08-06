import streamlit as st
from utils import qa_agent, QwenChat, QwenEmbeddings
from langchain.memory import ConversationBufferMemory
from langchain_core.messages import HumanMessage, AIMessage
import logging
import traceback
import os
from PIL import Image
import time
from typing import Dict, Any, Optional
import pandas as pd
from datetime import datetime
import shutil
import threading

# ==================== 初始化设置 ====================
st.set_page_config(
    page_title="智能单片机问答工具",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 初始化日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ==================== 统计模块 ====================
class ExcelTracker:
    def __init__(self):
        self.excel_path = os.path.abspath("学习记录.xlsx")
        self._lock = threading.Lock()
        self._backup_dir = os.path.abspath("backup")

        # 确保目录存在
        os.makedirs(self._backup_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.excel_path), exist_ok=True)

        self._init_excel_file()

    def _init_excel_file(self):
        """确保Excel文件存在并初始化"""
        with self._lock:
            if not os.path.exists(self.excel_path):
                logger.info("Excel文件不存在，正在初始化...")
                try:
                    self._create_new_excel_file()
                    logger.info("Excel文件初始化成功")
                except Exception as e:
                    logger.error(f"Excel文件初始化失败: {str(e)}")
                    raise
            else:
                logger.info("Excel文件已存在")

    def _create_new_excel_file(self):
        """创建全新的Excel文件"""
        try:
            with pd.ExcelWriter(self.excel_path, engine='openpyxl') as writer:
                # 学习记录表
                pd.DataFrame(columns=[
                    "学号", "问题类型", "问题内容", "MCU型号", "记录时间"
                ]).to_excel(writer, sheet_name="学习记录", index=False)

                # 统计摘要表
                pd.DataFrame(columns=[
                    "学号", "提问总数", "引脚问题", "中断问题",
                    "通信问题", "编程问题", "原理问题", "其他问题",
                    "MCU_51问题", "MCU_32问题"
                ]).to_excel(writer, sheet_name="统计摘要", index=False)
            logger.info("新Excel文件创建成功")
        except Exception as e:
            logger.error(f"创建新Excel文件失败: {str(e)}")
            raise

    def _create_backup(self):
        """创建备份文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(self._backup_dir, f"学习记录_备份_{timestamp}.xlsx")
        shutil.copy2(self.excel_path, backup_path)
        return backup_path

    def _is_duplicate_record(self, df, new_record, time_window=60):
        """检查是否为重复记录"""
        current_time = pd.to_datetime(new_record["记录时间"])
        time_threshold = current_time - pd.Timedelta(seconds=time_window)

        mask = (
                (df["学号"] == new_record["学号"]) &
                (df["问题内容"] == new_record["问题内容"]) &
                (pd.to_datetime(df["记录时间"]) >= time_threshold)
        )
        return mask.any()

    def record_question(self, student_id: str, question: str, mcu_model: str, answer: Optional[str] = None):
        """记录问题（线程安全版）"""
        if not student_id or not question:
            return

        with self._lock:
            try:
                if not os.path.exists(self.excel_path):
                    self._create_new_excel_file()

                backup_path = self._create_backup()
                logger.info(f"创建备份: {backup_path}")

                try:
                    df = pd.read_excel(self.excel_path, sheet_name="学习记录")
                except:
                    df = pd.DataFrame(columns=[
                        "学号", "问题类型", "问题内容", "MCU型号", "记录时间"
                    ])

                new_record = {
                    "学号": student_id,
                    "问题类型": self._classify_question(question),
                    "问题内容": question,
                    "MCU型号": mcu_model,
                    "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

                if not self._is_duplicate_record(df, new_record):
                    df = pd.concat([df, pd.DataFrame([new_record])], ignore_index=True)
                    self._save_and_update(df)
                    logger.info(f"问题记录成功: {student_id} - {question[:20]}...")
            except Exception as e:
                logger.error(f"问题记录失败: {str(e)}", exc_info=True)

    def _classify_question(self, question: str) -> str:
        """问题分类逻辑"""
        question = question.lower()
        if any(w in question for w in ["引脚", "管脚", "io"]):
            return "引脚问题"
        elif any(w in question for w in ["中断", "定时器"]):
            return "中断问题"
        elif any(w in question for w in ["串口", "uart", "通信"]):
            return "通信问题"
        elif any(w in question for w in ["程序", "代码"]):
            return "编程问题"
        elif any(w in question for w in ["原理", "工作"]):
            return "原理问题"
        return "其他问题"

    def _generate_summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成统计摘要（合并同一学生的所有记录）"""
        if df.empty:
            return pd.DataFrame(columns=[
                "学号", "提问总数", "引脚问题", "中断问题",
                "通信问题", "编程问题", "原理问题", "其他问题",
                "MCU_51问题", "MCU_32问题"
            ])

        # 确保合并前先按学号分组
        summary = df.groupby('学号').agg(
            提问总数=('学号', 'size'),  # 使用size计算总提问数
            引脚问题=('问题类型', lambda x: (x == '引脚问题').sum()),
            中断问题=('问题类型', lambda x: (x == '中断问题').sum()),
            通信问题=('问题类型', lambda x: (x == '通信问题').sum()),
            编程问题=('问题类型', lambda x: (x == '编程问题').sum()),
            原理问题=('问题类型', lambda x: (x == '原理问题').sum()),
            其他问题=('问题类型', lambda x: (x == '其他问题').sum()),
            MCU_51问题=('MCU型号', lambda x: (x == '51单片机').sum()),
            MCU_32问题=('MCU型号', lambda x: (x == '32单片机').sum())
        ).reset_index()

        return summary

    def _save_and_update(self, df):
        """修复后的保存方法"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.excel_path), exist_ok=True)

            # 使用临时文件
            temp_dir = os.path.dirname(self.excel_path)
            temp_filename = f"temp_{os.path.basename(self.excel_path)}"
            temp_path = os.path.join(temp_dir, temp_filename)

            with pd.ExcelWriter(temp_path, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name="学习记录", index=False)
                summary = self._generate_summary(df)
                summary.to_excel(writer, sheet_name="统计摘要", index=False)

            # 原子替换
            if os.path.exists(temp_path):
                if os.path.exists(self.excel_path):
                    os.remove(self.excel_path)
                os.rename(temp_path, self.excel_path)
                logger.info("数据保存成功")
            else:
                logger.error("临时文件创建失败")

        except Exception as e:
            logger.error(f"数据保存失败: {str(e)}", exc_info=True)
            # 尝试恢复备份
            backups = [f for f in os.listdir(self._backup_dir) if f.endswith('.xlsx')]
            if backups:
                backups.sort(key=lambda x: os.path.getctime(os.path.join(self._backup_dir, x)), reverse=True)
                latest_backup = os.path.join(self._backup_dir, backups[0])
                try:
                    shutil.copy2(latest_backup, self.excel_path)
                    logger.info(f"已恢复备份: {latest_backup}")
                except Exception as backup_error:
                    logger.error(f"备份恢复失败: {str(backup_error)}")

    def show_dashboard(self):
        """统计面板显示"""
        try:
            with self._lock:
                if not os.path.exists(self.excel_path):
                    self._create_new_excel_file()

                try:
                    summary_df = pd.read_excel(self.excel_path, sheet_name="统计摘要")
                    detail_df = pd.read_excel(self.excel_path, sheet_name="学习记录")
                except:
                    summary_df = pd.DataFrame()
                    detail_df = pd.DataFrame()

                st.title("📊 学情统计看板")

                if st.session_state.get('is_admin', False):
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("🔄 刷新数据", use_container_width=True):
                            st.rerun()
                    with col2:
                        if st.button("🗑️ 清空所有数据", type="primary", use_container_width=True):
                            self.clear_all_data()
                            st.rerun()

                tab1, tab2 = st.tabs(["统计摘要", "详细记录"])

                with tab1:
                    if not summary_df.empty:
                        sort_col = st.selectbox("排序依据", summary_df.columns[1:], index=0)
                        st.dataframe(
                            summary_df.sort_values(sort_col, ascending=False),
                            column_config={
                                "学号": "学生ID",
                                "提问总数": st.column_config.NumberColumn("提问总数"),
                                "MCU_51问题": "51单片机问题",
                                "MCU_32问题": "32单片机问题"
                            },
                            use_container_width=True,
                            height=600
                        )
                    else:
                        st.warning("没有统计摘要数据")

                with tab2:
                    if not detail_df.empty:
                        col1, col2 = st.columns(2)
                        with col1:
                            student_filter = st.selectbox(
                                "按学号筛选",
                                ["全部"] + list(detail_df["学号"].unique())
                            )
                        with col2:
                            mcu_filter = st.selectbox(
                                "按MCU型号筛选",
                                ["全部"] + list(detail_df["MCU型号"].unique())
                            )

                        filtered_df = detail_df
                        if student_filter != "全部":
                            filtered_df = filtered_df[filtered_df["学号"] == student_filter]
                        if mcu_filter != "全部":
                            filtered_df = filtered_df[filtered_df["MCU型号"] == mcu_filter]

                        st.dataframe(
                            filtered_df,
                            hide_index=True,
                            use_container_width=True,
                            height=600
                        )
                    else:
                        st.warning("没有详细记录数据")

        except Exception as e:
            st.error(f"加载数据失败: {str(e)}")

    def clear_all_data(self):
        """清空所有数据"""
        with self._lock:
            try:
                self._create_new_excel_file()
                st.success("所有数据已清空！")
                logger.info("数据清空操作完成")
            except Exception as e:
                st.error(f"清空失败: {str(e)}")
                logger.error(f"清空数据错误: {str(e)}", exc_info=True)


# 全局统计实例
tracker = ExcelTracker()


# ==================== 状态管理系统 ====================
class AppState:
    @staticmethod
    def init():
        """初始化所有会话状态"""
        if 'app_state' not in st.session_state:
            st.session_state.app_state = {
                'is_admin': False,
                'show_stats': False,
                'switch_time': time.time(),
                'memory': ConversationBufferMemory(
                    return_messages=True,
                    memory_key="chat_history",
                    output_key="answer"
                ),
                'messages': [],
                'mcu_model': "51单片机"
            }
            st.session_state.app_state['memory'].chat_memory.add_message(
                AIMessage(content="你好！我是单片机助手，请问你有什么问题？")
            )

    @staticmethod
    def update(**kwargs):
        """更新状态并记录切换时间"""
        st.session_state.app_state.update({
            **kwargs,
            'switch_time': time.time()
        })


AppState.init()

# ==================== 性能优化CSS ====================
st.markdown("""
    <style>
    /* 界面切换动画 */
    .stApp {
        transition: opacity 0.3s ease;
    }
    /* 按钮响应优化 */
    .stButton>button {
        transition: transform 0.1s ease;
    }
    .stButton>button:hover {
        transform: scale(1.02);
    }
    /* 输入框优化 */
    .stTextInput>div>div>input {
        padding: 8px 12px !important;
    }
    /* 侧边栏优化 */
    .sidebar .stButton>button {
        width: 100%;
    }
    </style>
""", unsafe_allow_html=True)


# ==================== 核心功能组件 ====================
def load_image(image_name: str) -> Image.Image:
    """优化后的图片加载函数"""
    paths = [image_name, f"images/{image_name}", f"static/{image_name}"]
    for path in paths:
        try:
            if os.path.exists(path):
                return Image.open(path)
        except Exception as e:
            logger.error(f"图片加载失败 {path}: {e}")
    return None


@st.cache_resource(ttl=3600)
def init_llm(api_key: str) -> QwenChat:
    """带缓存的LLM初始化"""
    return QwenChat(api_key=api_key, model_name="qwen-plus")


@st.cache_resource(ttl=3600)
def init_embeddings(api_key: str) -> QwenEmbeddings:
    """带缓存的Embeddings初始化"""
    return QwenEmbeddings(api_key=api_key)


# ==================== 界面组件 ====================
def show_config_panel():
    """配置面板组件"""
    with st.expander("🔧 配置选项", expanded=False):
        col1, col2 = st.columns([3, 2])
        with col1:
            qwen_api_key = st.text_input(
                "通义千问API密钥",
                type="password",
                placeholder="输入API密钥",
                key="api_key"
            )
        with col2:
            student_id = st.text_input(
                "学号",
                placeholder="请输入学号",
                key="student_id"
            )

        # 单片机型号选择
        st.markdown("选择单片机型号:")
        mcu_col1, mcu_col2 = st.columns(2)

        with mcu_col1:
            container = st.container()
            img_51 = load_image("51.jpg")
            if img_51:
                container.image(img_51, width=150, caption="51单片机", use_container_width=False)
            else:
                container.warning("51单片机图片未找到")
            if container.button("51单片机", key="btn_51"):
                AppState.update(mcu_model="51单片机")
                st.rerun()

        with mcu_col2:
            container = st.container()
            img_32 = load_image("32.jpg")
            if img_32:
                container.image(img_32, width=150, caption="32单片机", use_container_width=False)
            else:
                container.warning("32单片机图片未找到")
            if container.button("32单片机", key="btn_32"):
                AppState.update(mcu_model="32单片机")
                st.rerun()

        st.markdown(f"**当前选择**: {st.session_state.app_state['mcu_model']}")

        if st.button("🔄 重置对话"):
            AppState.update(
                memory=ConversationBufferMemory(
                    return_messages=True,
                    memory_key="chat_history",
                    output_key="answer"
                ),
                messages=[]
            )
            st.session_state.app_state['memory'].chat_memory.add_message(
                AIMessage(content="对话已重置，请重新提问。")
            )
            st.rerun()


def show_admin_panel():
    """管理员控制面板"""
    with st.sidebar:
        if not st.session_state.app_state['is_admin']:
            password = st.text_input("管理员密码", type="password", key="admin_pw")
            if password == "qwert":
                AppState.update(is_admin=True, show_stats=True)
                st.rerun()
            elif password:
                st.error("密码错误")
        else:
            st.success("管理员模式")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("📊 统计面板"):
                    AppState.update(show_stats=True)
            with col2:
                if st.button("❓ 问答界面"):
                    AppState.update(show_stats=False)
            if st.button("🚪 退出管理", type="primary"):
                AppState.update(is_admin=False, show_stats=False)
            st.metric("响应延迟", f"{(time.time() - st.session_state.app_state['switch_time']):.3f}s")


# ==================== 主界面路由 ====================
def show_stats_interface():
    """统计面板界面"""
    with st.spinner("正在加载数据..."):
        tracker.show_dashboard()
    if st.button("← 返回问答界面"):
        AppState.update(show_stats=False)


def show_qa_interface():
    """主问答界面"""
    st.title("🖥️ 单片机智能问答")

    student_id = st.session_state.get('student_id', '')
    if student_id:
        st.info(f"当前学号: {student_id}")

    question = st.chat_input("输入您的问题...")
    if question:
        api_key = st.session_state.get('api_key', '')
        if not api_key:
            st.error("请先输入API密钥")
            return

        with st.spinner("正在思考..."):
            try:
                llm = init_llm(api_key)
                embeddings = init_embeddings(api_key)

                result = qa_agent(
                    question=question,
                    memory=st.session_state.app_state['memory'],
                    mcu_model=st.session_state.app_state['mcu_model'],
                    llm=llm,
                    embeddings=embeddings
                )

                answer = result.get("answer", "抱歉，我没有找到答案。")
                st.session_state.app_state['messages'].append(
                    {"role": "assistant", "content": answer}
                )

                if student_id:
                    tracker.record_question(
                        student_id=student_id,
                        question=question,
                        mcu_model=st.session_state.app_state['mcu_model'],
                        answer=answer[:100]
                    )

            except Exception as e:
                error_msg = f"系统错误: {str(e)}"
                st.error(error_msg)
                logger.error(traceback.format_exc())

    for msg in st.session_state.app_state['messages'][-10:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


# ==================== 应用主入口 ====================
def main():
    show_config_panel()
    show_admin_panel()

    if st.session_state.app_state['show_stats']:
        show_stats_interface()
    else:
        show_qa_interface()


if __name__ == "__main__":
    main()