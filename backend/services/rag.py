# backend/services/rag.py
from langchain_aws import BedrockEmbeddings, ChatBedrock
from langchain_community.vectorstores import FAISS
from langchain.chains import ConversationalRetrievalChain

class ExpenditureRAG:
    def __init__(self):
        self.embeddings = BedrockEmbeddings(model_id='amazon.titan-embed-text-v1')
        self.llm        = ChatBedrock(model_id='anthropic.claude-sonnet-4-5')
        self.vectorstore = None

    # 사용자 지출 데이터 → 벡터DB 인덱싱
    def index_expenses(self, expenses: list[dict]):
        docs = [
            f"{e['date']} {e['store_name']} {e['category']} {e['amount']}원"
            for e in expenses
        ]
        self.vectorstore = FAISS.from_texts(docs, self.embeddings)

    # 질의응답
    def chat(self, query: str, chat_history: list) -> str:
        chain = ConversationalRetrievalChain.from_llm(
            llm=self.llm,
            retriever=self.vectorstore.as_retriever(search_kwargs={"k": 5}),
            return_source_documents=False
        )
        res = chain({"question": query, "chat_history": chat_history})
        return res['answer']

# 사용 예시
# "이번 달 식비가 얼마야?"
# "지난달이랑 비교해서 어디서 많이 썼어?"
# "이번 주 카페 지출 목록 보여줘"