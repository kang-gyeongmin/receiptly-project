from motor.motor_asyncio import AsyncIOMotorClient

class MongoDB:
    def __init__(self):
        self.client = AsyncIOMotorClient(os.getenv('MONGO_URI'))
        self.db     = self.client['receiptly']

    # 영수증 원본 저장 (비정형)
    async def save_receipt(self, receipt: dict) -> str:
        res = await self.db.receipts.insert_one(receipt)
        return str(res.inserted_id)