import boto3
import json

class CategoryClassifier:
    CATEGORIES = ['식비', '교통', '쇼핑', '의료', '문화', '기타']

    def __init__(self):
        self.runtime = boto3.client('sagemaker-runtime')
        self.endpoint = 'receiptly-classifier-endpoint'

    # SageMaker 엔드포인트 호출
    def predict(self, store_name: str, items: list) -> str:
        input_text = f"{store_name} {' '.join([i['name'] for i in items])}"
        res = self.runtime.invoke_endpoint(
            EndpointName=self.endpoint,
            ContentType='text/csv',
            Body=input_text
        )
        return json.loads(res['Body'].read())['predicted_label']

    # SageMaker 없을 때 LLM으로 폴백
    def predict_with_llm(self, store_name: str, items: list) -> str:
        prompt = f"""
        가게명: {store_name}
        구매 항목: {items}
        
        위 영수증의 카테고리를 아래 중 하나로만 답해:
        {self.CATEGORIES}
        """
        # Bedrock 호출
        ...