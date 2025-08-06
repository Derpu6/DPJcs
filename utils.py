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

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== 路径配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # 获取当前文件绝对路径
VECTORSTORE_DIR = os.path.join(BASE_DIR, "vectorstores")  # 向量库存储路径
PDF_DIR = os.path.join(BASE_DIR, "data")  # PDF文档存储路径

# 确保目录存在
os.makedirs(VECTORSTORE_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)

# ==================== 1. Embedding 模型封装 ====================
class QwenEmbeddings(Embeddings):
    """通义千问文本向量化"""

    def __init__(self, api_key: str, model: str = "text-embedding-v2"):
        if not api_key or not api_key.startswith("sk-"):
            raise ValueError("无效的API密钥格式")
        dashscope.api_key = api_key
        self.model = model

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            embeddings = []
            for text in texts:
                resp = dashscope.TextEmbedding.call(
                    model=self.model,
                    input=text,
                    timeout=15
                )
                if resp and resp.status_code == 200:
                    embeddings.append(resp.output["embeddings"][0]["embedding"])
                else:
                    msg = getattr(resp, 'message', '未知错误')
                    raise ValueError(f"Embedding失败: {msg}")
            return embeddings
        except Exception as e:
            logger.error(f"Embedding错误: {str(e)}")
            raise

    def embed_query(self, text: str) -> List[float]:
        try:
            resp = dashscope.TextEmbedding.call(
                model=self.model,
                input=text,
                timeout=15
            )
            if resp and resp.status_code == 200:
                return resp.output["embeddings"][0]["embedding"]
            msg = getattr(resp, 'message', '未知错误')
            raise ValueError(f"Embedding失败: {msg}")
        except Exception as e:
            logger.error(f"Embedding查询错误: {str(e)}")
            raise

# ==================== 2. LLM 模型封装 ====================
class QwenChat(BaseChatModel):
    """通义千问对话模型（兼容LangChain）"""
    model_name: str = "qwen-plus"
    temperature: float = 0.3
    api_key: str

    def _generate(
            self,
            messages: List[HumanMessage | AIMessage],
            stop: Optional[List[str]] = None,
            run_manager: Optional[Any] = None,
            **kwargs
    ) -> ChatResult:
        if not messages:
            raise ValueError("消息列表不能为空")
        if not self.api_key:
            raise ValueError("API密钥未设置")

        dashscope.api_key = self.api_key

        # 转换消息
        qwen_messages = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                qwen_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                qwen_messages.append({"role": "assistant", "content": msg.content})

        try:
            resp = dashscope.Generation.call(
                model=self.model_name,
                messages=qwen_messages,
                temperature=self.temperature,
                top_p=0.8,
                max_tokens=1024,
                timeout=30,
                **kwargs
            )

            if not resp:
                raise ValueError("API返回空响应")
            if resp.status_code != 200:
                raise ValueError(f"API错误: {resp.code} - {resp.message}")
            if not hasattr(resp, 'output') or not resp.output:
                raise ValueError("API响应格式错误")

            # 安全提取文本
            output = resp.output
            if isinstance(output, dict):
                if "text" in output:
                    content = output["text"]
                elif "choices" in output and len(output["choices"]) > 0:
                    content = output["choices"][0]["message"]["content"]
                else:
                    raise ValueError("无法提取回答内容")
            else:
                raise ValueError("output 格式错误")

            generation = ChatGeneration(
                message=AIMessage(content=content),
                text=content
            )

            return ChatResult(generations=[generation])

        except Exception as e:
            logger.error(f"API调用异常: {str(e)}")
            raise ValueError(f"生成失败: {str(e)}")

    @property
    def _llm_type(self) -> str:
        return "qwen-chat"

# ==================== 3. 缓存向量数据库 ====================
def get_or_create_retriever(
    mcu_model: str,
    embeddings: Embeddings,
    pdf_dir: str = PDF_DIR  # 使用全局PDF_DIR
) -> Any:
    """
    获取或创建向量检索器（带本地缓存）
    """
    pdf_path = os.path.join(pdf_dir, f"{mcu_model}.pdf")
    vectorstore_path = os.path.join(VECTORSTORE_DIR, mcu_model)

    # 检查是否已缓存向量库
    if os.path.exists(vectorstore_path):
        logger.info(f"正在加载缓存的向量数据库: {vectorstore_path}")
        db = FAISS.load_local(
            vectorstore_path,
            embeddings,
            allow_dangerous_deserialization=True  # 注意安全
        )
        return db.as_retriever(search_kwargs={"k": 3})

    # 首次处理：加载、切分、向量化、保存
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"找不到资料文件: {pdf_path}")

    logger.info(f"首次处理文档，正在构建向量数据库: {pdf_path}")
    loader = PyPDFLoader(pdf_path)
    docs = loader.load()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=100,
        separators=["\n\n", "\n", "。", "！", "？", "；", ".", ";", " ", ""]
    )
    texts = text_splitter.split_documents(docs)

    # 构建并向量化
    db = FAISS.from_documents(texts, embeddings)

    # 保存到本地
    os.makedirs(VECTORSTORE_DIR, exist_ok=True)
    db.save_local(vectorstore_path)
    logger.info(f"向量数据库已保存至: {vectorstore_path}")

    return db.as_retriever(search_kwargs={"k": 3})

# ==================== 4. 问答代理 ====================
def qa_agent(
    question: str,
    memory: ConversationBufferMemory,
    mcu_model: str,
    llm: BaseChatModel,
    embeddings: Embeddings
) -> Dict[str, Any]:
    """问答代理（使用缓存向量库）"""
    try:
        if not question.strip():
            raise ValueError("问题不能为空")

        # 使用缓存的检索器
        retriever = get_or_create_retriever(mcu_model, embeddings)

        # 自定义 Prompt
        prompt = ChatPromptTemplate.from_template("""
你是一个单片机技术文档助手，请严格根据以下检索到的上下文回答问题。
回答要简洁、准确，使用中文。

如果上下文中有明确答案，你先回答出得到的答案，再讲上下文中有关这个部分的全部内容，最好结合相关代码，最后扩展与上下文无关的内容,与上下文无关的内容可以使用比喻等方法让文字更加活泼易懂，其他部分保持严谨。

如果上下文中没有明确答案，请先回答“我没有找到相关信息”，在补充从网上寻找到的的内容,这时候可以说的详细一点,与上下文无关的内容可以使用比喻等方法让文字更加活泼易懂。

请不要编造或推测不存在的东西。

上下文:
{context}

历史对话:
{chat_history}

当前问题:
{input}
""")

        # 创建文档链和检索链
        document_chain = create_stuff_documents_chain(llm=llm, prompt=prompt)
        retrieval_chain = create_retrieval_chain(retriever=retriever, combine_docs_chain=document_chain)

        # 提取历史对话
        chat_history = ""
        if memory.chat_memory.messages:
            chat_history = get_buffer_string(memory.chat_memory.messages)

        # 执行查询
        result = retrieval_chain.invoke({
            "input": question,
            "chat_history": chat_history
        })

        if "answer" not in result:
            raise ValueError("无效的响应格式")

        # 保存到记忆
        memory.save_context(
            {"input": question},
            {"answer": result["answer"]}
        )

        return {"answer": result["answer"]}

    except Exception as e:
        logger.error(f"问答流程错误: {str(e)}")
        return {"answer": "抱歉，我在处理问题时遇到了错误。"}
