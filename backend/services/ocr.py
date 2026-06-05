import boto3
import json

class ReceiptProcessor:
    def __init__(self):
        self.textract = boto3.client('textract', region_name='ap-northeast-2')
        self.bedrock  = boto3.client('bedrock-runtime', region_name='ap-northeast-2')

    # 이미지 → 텍스트 (AWS Textract)
    def extract_text(self, image_bytes: bytes) -> str:
        res = self.textract.detect_document_text(
            Document={'Bytes': image_bytes}
        )
        lines = [
            block['Text']
            for block in res['Blocks']
            if block['BlockType'] == 'LINE'
        ]
        return '\n'.join(lines)

    # 텍스트 → 구조화 데이터 (LLM)
    def parse_receipt(self, raw_text: str) -> dict:
        prompt = f"""
        다음 영수증 텍스트에서 정보를 추출해서 JSON으로 반환해줘.
        반드시 아래 형식만 반환해:
        {{
            "store_name": "가게명",
            "date": "YYYY-MM-DD",
            "total_amount": 숫자,
            "items": [{{"name": "상품명", "price": 숫자}}]
        }}

        영수증:
        {raw_text}
        """
        res = self.bedrock.invoke_model(
            modelId='anthropic.claude-sonnet-4-5',
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            })
        )
        content = json.loads(res['body'].read())
        return json.loads(content['content'][0]['text'])