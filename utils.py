from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage, get_buffer_string
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain.memory import ConversationBufferMemory
from typing import List, Dict, Any, Optional
import dashscope
import os
import logging
import traceback
from pathlib import Path

# ==================== 初始化设置 ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 路径配置（兼容本地和Streamlit Cloud）
BASE_DIR = Path("/mount/src/dpjcs") if "MOUNT_SRC" in os.environ else Path(__file__).parent.absolute()
VECTORSTORE_DIR = BASE_DIR / "vectorstores"
PDF_DIR = BASE_DIR / "data"

# 确保目录存在
os.makedirs(VECTORSTORE_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)

# ==================== 1. Embedding 模型封装 ====================
class QwenEmbeddings(Embeddings):
    """增强版的通义千问文本向量化"""
    def __init__(self, api_key: str, model: str = "text-embedding-v2"):
        if not api_key or not api_key.startswith("sk-"):
            raise ValueError("无效的API密钥格式")
        dashscope.api_key = api_key
        self.model = model

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        
        embeddings = []
        for text in texts:
            try:
                resp = dashscope.TextEmbedding.call(
                    model=self.model,
                    input=text,
                    timeout=15
                )
                if resp.status_code == 200:
                    embeddings.append(resp.output["embeddings"][0]["embedding"])
                else:
                    logger.error(f"Embedding失败: {resp.code} - {resp.message}")
                    raise ValueError(f"API错误: {resp.message}")
            except Exception as e:
                logger.error(f"文本向量化失败: {str(e)}\n文本内容: {text[:50]}...")
                raise
        return embeddings

    def embed_query(self, text: str) -> List[float]:
        try:
            resp = dashscope.TextEmbedding.call(
                model=self.model,
                input=text,
                timeout=15
            )
            if resp.status_code == 200:
                return resp.output["embeddings"][0]["embedding"]
            logger.error(f"查询向量化失败: {resp.code} - {resp.message}")
            raise ValueError(f"API错误: {resp.message}")
        except Exception as e:
            logger.error(f"查询向量化异常: {str(e)}")
            raise

# ==================== 2. LLM 模型封装 ====================
class QwenChat(BaseChatModel):
    """增强版的通义千问对话模型"""
    model_name: str = "qwen-plus"
    temperature: float = 0.3
    api_key: str
    max_retries: int = 3

    def _generate(self, messages: List[HumanMessage | AIMessage], **kwargs) -> ChatResult:
        for attempt in range(self.max_retries):
            try:
                qwen_messages = [
                    {"role": "user" if isinstance(msg, HumanMessage) else "assistant", 
                     "content": msg.content}
                    for msg in messages
                ]
                
                resp = dashscope.Generation.call(
                    model=self.model_name,
                    messages=qwen_messages,
                    temperature=self.temperature,
                    top_p=0.8,
                    max_tokens=1024,
                    timeout=30,
                    **kwargs
                )

                if resp.status_code != 200:
                    raise ValueError(f"API错误: {resp.code} - {resp.message}")

                content = self._extract_content(resp.output)
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content=content),
                    text=content
                )])

            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"API调用最终失败: {str(e)}\n{traceback.format_exc()}")
                    raise
                logger.warning(f"API调用失败，重试 {attempt + 1}/{self.max_retries}: {str(e)}")
                continue

    def _extract_content(self, output: Dict) -> str:
        """安全提取响应内容"""
        if isinstance(output, dict):
            if "text" in output:
                return output["text"]
            elif "choices" in output and output["choices"]:
                return output["choices"][0]["message"]["content"]
        raise ValueError("无法解析API响应内容")

    @property
    def _llm_type(self) -> str:
        return "qwen-chat"

# ==================== 3. 向量数据库优化 ====================
def get_or_create_retriever(mcu_model: str, embeddings: Embeddings) -> Any:
    """增强版的向量检索器获取"""
    try:
        pdf_path = PDF_DIR / f"{mcu_model}.pdf"
        vectorstore_path = VECTORSTORE_DIR / mcu_model
        
        # 检查必要文件是否存在
        required_files = ['index.faiss', 'index.pkl']
        if all((vectorstore_path / f).exists() for f in required_files):
            try:
                return FAISS.load_local(
                    str(vectorstore_path),
                    embeddings,
                    allow_dangerous_deserialization=True
                ).as_retriever(search_kwargs={"k": 3})
            except Exception as e:
                logger.error(f"向量库加载失败，将重建: {str(e)}")
                # 清理损坏的文件
                for f in required_files:
                    (vectorstore_path / f).unlink(missing_ok=True)
        
        # 重建向量库
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        logger.info(f"构建新的向量数据库: {pdf_path}")
        loader = PyPDFLoader(str(pdf_path))
        docs = loader.load_and_split(
            text_splitter=RecursiveCharacterTextSplitter(
                chunk_size=500,
                chunk_overlap=100,
                separators=["\n\n", "\n", "。", "！", "？", ";", ".", " "]
            )
        )

        db = FAISS.from_documents(docs, embeddings)
        db.save_local(str(vectorstore_path))
        return db.as_retriever(search_kwargs={"k": 3})

    except Exception as e:
        logger.error(f"向量检索器创建失败: {str(e)}\n{traceback.format_exc()}")
        raise ValueError(f"无法创建检索器: {str(e)}")

# ==================== 4. 问答代理优化 ====================
def qa_agent(
    question: str,
    memory: ConversationBufferMemory,
    mcu_model: str,
    llm: BaseChatModel,
    embeddings: Embeddings
) -> Dict[str, Any]:
    """增强版的问答代理"""
    try:
        question = question.strip()
        if not question:
            return {"answer": "问题不能为空"}

        # 获取检索器
        retriever = get_or_create_retriever(mcu_model, embeddings)

        # 构建问答链
        prompt = ChatPromptTemplate.from_template("""
你是一个专业的单片机技术助手，请根据以下上下文回答问题。
上下文:
{context}

历史对话:
{chat_history}

当前问题: {input}

回答要求:
1. 如果上下文有明确答案，先直接回答
2. 然后解释相关原理
3. 最后提供示例代码(如果有)
4. 保持专业但易懂的风格
5. 如果不知道，明确说明并给出建议
""")
        
        document_chain = create_stuff_documents_chain(llm, prompt)
        retrieval_chain = create_retrieval_chain(retriever, document_chain)

        # 执行查询
        result = retrieval_chain.invoke({
            "input": question,
            "chat_history": get_buffer_string(memory.chat_memory.messages)
        })

        # 保存对话
        memory.save_context(
            {"input": question},
            {"answer": result.get("answer", "未获得有效回答")}
        )

        return {"answer": result.get("answer", "抱歉，我无法回答这个问题")}

    except FileNotFoundError as e:
        logger.error(f"文件缺失错误: {str(e)}")
        return {
            "answer": "系统资源未正确初始化",
            "debug": f"请确认已上传{PDF_DIR}/{mcu_model}.pdf文件"
        }
    except Exception as e:
        logger.error(f"系统错误: {traceback.format_exc()}")
        return {"answer": "系统处理问题时出错，请稍后再试"}
